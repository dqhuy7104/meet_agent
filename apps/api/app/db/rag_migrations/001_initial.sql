CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE IF NOT EXISTS rag_embedding_profiles (
    id text PRIMARY KEY, definition jsonb NOT NULL, ready boolean NOT NULL DEFAULT false
);
CREATE TABLE IF NOT EXISTS rag_publications (
    profile_id text REFERENCES rag_embedding_profiles(id) ON DELETE CASCADE,
    meeting_id text NOT NULL, revision text NOT NULL, PRIMARY KEY(profile_id, meeting_id)
);
CREATE TABLE IF NOT EXISTS rag_chunks (
    profile_id text REFERENCES rag_embedding_profiles(id) ON DELETE CASCADE,
    source_id text NOT NULL, meeting_id text NOT NULL, revision text NOT NULL,
    ordinal integer NOT NULL CHECK(ordinal >= 0), text text NOT NULL, payload jsonb NOT NULL,
    document tsvector GENERATED ALWAYS AS (to_tsvector('simple', text)) STORED,
    PRIMARY KEY(profile_id, source_id),
    FOREIGN KEY(profile_id, meeting_id) REFERENCES rag_publications(profile_id, meeting_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS rag_chunks_document ON rag_chunks USING gin(document);
CREATE INDEX IF NOT EXISTS rag_chunks_meeting ON rag_chunks(profile_id, meeting_id);
CREATE TABLE IF NOT EXISTS rag_chunk_embeddings (
    profile_id text NOT NULL, source_id text NOT NULL, embedding vector NOT NULL,
    dimensions integer NOT NULL CHECK(dimensions > 0),
    PRIMARY KEY(profile_id, source_id),
    FOREIGN KEY(profile_id, source_id) REFERENCES rag_chunks(profile_id, source_id) ON DELETE CASCADE,
    CHECK(vector_dims(embedding) = dimensions)
);
CREATE TABLE IF NOT EXISTS rag_index_jobs (
    profile_id text REFERENCES rag_embedding_profiles(id) ON DELETE CASCADE,
    meeting_id text NOT NULL, revision text NOT NULL, state text NOT NULL DEFAULT 'pending',
    attempts integer NOT NULL DEFAULT 0, failure_code text, lease_until timestamptz,
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(profile_id, meeting_id)
);
