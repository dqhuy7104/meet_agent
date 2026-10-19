"""Transactional, profile-isolated derived index and publication manifests."""
import json
from dataclasses import asdict
from psycopg import Connection
from psycopg.types.json import Jsonb
from app.core.rag_config import EmbeddingProfile, RagError
from app.services.rag.providers import validate_vectors
from app.services.rag.types import Candidate, MeetingSnapshot, SourceChunk, SourceSegment


def _chunk(payload: dict) -> SourceChunk:
    return SourceChunk(**(payload | {'source_segments': tuple(SourceSegment(**s) for s in payload['source_segments'])}))


class RagRepository:
    def __init__(self, connection: Connection) -> None:
        self.connection = connection

    def register_profiles(self, profiles: list[EmbeddingProfile]) -> None:
        with self.connection.transaction():
            for profile in profiles:
                definition = profile.model_dump()
                self.connection.execute('INSERT INTO rag_embedding_profiles(id, definition) VALUES (%s,%s) ON CONFLICT DO NOTHING', (profile.id, Jsonb(definition)))
                stored = self.connection.execute('SELECT definition FROM rag_embedding_profiles WHERE id=%s FOR UPDATE', (profile.id,)).fetchone()
                if stored[0] != definition:
                    raise RagError('immutable_profile_changed', 409)

    def retire_profiles(self, active_ids: list[str]) -> None:
        with self.connection.transaction():
            self.connection.execute('DELETE FROM rag_embedding_profiles WHERE NOT (id = ANY(%s))', (active_ids,))

    def manifest(self, profile_id: str, meeting_id: str) -> str | None:
        row = self.connection.execute('SELECT revision FROM rag_publications WHERE profile_id=%s AND meeting_id=%s', (profile_id, meeting_id)).fetchone()
        return row[0] if row else None

    def manifests(self) -> list[tuple[str, str, str]]:
        return self.connection.execute('SELECT profile_id, meeting_id, revision FROM rag_publications').fetchall()

    def publish(self, snapshot: MeetingSnapshot, profile: EmbeddingProfile, chunks: list[SourceChunk], vectors: list[list[float]]) -> None:
        validate_vectors(vectors, len(chunks), profile.dimensions)
        with self.connection.transaction():
            self.register_profiles([profile])
            self.connection.execute('DELETE FROM rag_publications WHERE profile_id=%s AND meeting_id=%s', (profile.id, snapshot.meeting_id))
            self.connection.execute('INSERT INTO rag_publications VALUES (%s,%s,%s)', (profile.id, snapshot.meeting_id, snapshot.revision))
            for chunk, vector in zip(chunks, vectors):
                self.connection.execute('INSERT INTO rag_chunks(profile_id,source_id,meeting_id,revision,ordinal,text,payload) VALUES (%s,%s,%s,%s,%s,%s,%s)',
                                        (profile.id, chunk.source_id, chunk.meeting_id, chunk.revision, chunk.ordinal, chunk.text, Jsonb(asdict(chunk))))
                self.connection.execute('INSERT INTO rag_chunk_embeddings VALUES (%s,%s,%s::vector,%s)', (profile.id, chunk.source_id, json.dumps(vector), profile.dimensions))
            self.connection.execute("UPDATE rag_index_jobs SET state='done', lease_until=NULL, failure_code=NULL WHERE profile_id=%s AND meeting_id=%s", (profile.id, snapshot.meeting_id))

    def purge(self, meeting_id: str) -> None:
        with self.connection.transaction():
            self.connection.execute('DELETE FROM rag_publications WHERE meeting_id=%s', (meeting_id,))
            self.connection.execute('DELETE FROM rag_index_jobs WHERE meeting_id=%s', (meeting_id,))

    def queue(self, profile: EmbeddingProfile, snapshot: MeetingSnapshot) -> None:
        with self.connection.transaction():
            self.connection.execute('UPDATE rag_embedding_profiles SET ready=false WHERE id=%s', (profile.id,))
            self.connection.execute('''INSERT INTO rag_index_jobs(profile_id,meeting_id,revision) VALUES (%s,%s,%s)
                ON CONFLICT(profile_id,meeting_id) DO UPDATE SET revision=excluded.revision, state='pending', attempts=0,
                failure_code=NULL, lease_until=NULL, next_attempt_at=now() WHERE rag_index_jobs.revision<>excluded.revision''',
                (profile.id, snapshot.meeting_id, snapshot.revision))

    def claim(self):
        with self.connection.transaction():
            row = self.connection.execute("""SELECT profile_id,meeting_id,revision FROM rag_index_jobs
                WHERE (state='pending' OR (state='running' AND lease_until<now())) AND next_attempt_at<=now()
                ORDER BY profile_id,meeting_id LIMIT 1 FOR UPDATE SKIP LOCKED""").fetchone()
            if row:
                self.connection.execute("UPDATE rag_index_jobs SET state='running', attempts=attempts+1, lease_until=now()+interval '5 minutes' WHERE profile_id=%s AND meeting_id=%s", row[:2])
            return row

    def fail_job(self, profile_id: str, meeting_id: str, code: str, transient: bool) -> None:
        self.connection.execute("""UPDATE rag_index_jobs SET failure_code=%s, lease_until=NULL,
            state=CASE WHEN %s AND attempts<3 THEN 'pending' ELSE 'failed' END,
            next_attempt_at=now()+CASE WHEN attempts=1 THEN interval '1 second' ELSE interval '5 seconds' END
            WHERE profile_id=%s AND meeting_id=%s""", (code, transient, profile_id, meeting_id))

    def mark_ready(self, profile_id: str) -> None:
        self.connection.execute("""UPDATE rag_embedding_profiles SET ready=NOT EXISTS
            (SELECT 1 FROM rag_index_jobs WHERE profile_id=%s AND state<>'done') WHERE id=%s""", (profile_id, profile_id))

    def is_profile_ready(self, profile_id: str) -> bool:
        row = self.connection.execute('SELECT ready FROM rag_embedding_profiles WHERE id=%s', (profile_id,)).fetchone()
        return bool(row and row[0])

    def search(self, profile_id: str, question: str, vector: list[float], meeting_ids: list[str] | None) -> tuple[list[Candidate], list[Candidate]]:
        definition = self.connection.execute('SELECT definition FROM rag_embedding_profiles WHERE id=%s', (profile_id,)).fetchone()
        if not definition:
            raise RagError('profile_not_ready', 409)
        validate_vectors([vector], 1, definition[0]['dimensions'])
        if meeting_ids == []:
            return [], []
        scope = '' if meeting_ids is None else ' AND c.meeting_id = ANY(%s)'
        parameters = (json.dumps(vector), profile_id) + (() if meeting_ids is None else (meeting_ids,))
        # Materialize only this profile before cosine distance; dimensions may differ elsewhere.
        base = '''WITH scoped AS MATERIALIZED (SELECT c.payload,c.source_id,c.document,e.embedding FROM rag_chunks c
            JOIN rag_chunk_embeddings e USING(profile_id,source_id) WHERE c.profile_id=%s''' + scope + ''')
            SELECT payload,1-(embedding <=> %s::vector) AS cosine FROM scoped'''
        parameters = (profile_id,) + (() if meeting_ids is None else (meeting_ids,)) + (json.dumps(vector),)
        vectors = self.connection.execute(base + ' ORDER BY cosine DESC,source_id LIMIT 50', parameters).fetchall()
        texts = self.connection.execute(base + " WHERE document @@ plainto_tsquery('simple', %s) ORDER BY ts_rank(document,plainto_tsquery('simple', %s)) DESC,source_id LIMIT 50", parameters + (question, question)).fetchall()
        return ([Candidate(_chunk(p), float(c)) for p, c in vectors], [Candidate(_chunk(p), float(c)) for p, c in texts])
