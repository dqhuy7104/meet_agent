from __future__ import annotations

from typing import Any
from collections.abc import Callable
from uuid import uuid4

from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.core.rag_config import RagError
from app.schemas.conversations import TurnRequest
from app.schemas.chat import ConversationConfig


class ConversationRepository:
    def __init__(self, connection: Connection) -> None:
        self.connection = connection

    def _rows(self, statement: str, parameters: tuple = ()) -> list[dict[str, Any]]:
        with self.connection.cursor(row_factory=dict_row) as cursor:
            return cursor.execute(statement, parameters).fetchall()

    def create(self, account_id: str, project_id: str | None) -> dict:
        return self._rows('INSERT INTO rag_conversations(creator_account_id,project_id) VALUES (%s,%s) RETURNING *',
                          (account_id,project_id))[0]

    def get(self, conversation_id: str) -> dict | None:
        rows = self._rows('''SELECT c.*,a.email AS creator_email,
            coalesce((SELECT max(ordinal) FROM rag_conversation_turns t WHERE t.conversation_id=c.id),0) AS last_ordinal
            FROM rag_conversations c JOIN accounts a ON a.id=c.creator_account_id WHERE c.id=%s''', (conversation_id,))
        return rows[0] if rows else None

    def list(self, account_id: str, others: bool, cursor: tuple | None = None, limit: int = 20) -> list[dict]:
        operator = '<>' if others else '='
        boundary = ' AND (c.updated_at,c.id)<(%s::timestamptz,%s)' if cursor else ''
        return self._rows('SELECT c.id FROM rag_conversations c WHERE creator_account_id '+operator+'%s'+boundary+
                          ' ORDER BY c.updated_at DESC,c.id DESC LIMIT %s', (account_id, *(cursor or ()), limit+1))

    def turns(self, conversation_id: str, before: int | None = None, limit: int = 50) -> list[dict]:
        return self._rows('SELECT * FROM rag_conversation_turns WHERE conversation_id=%s AND ordinal<%s ORDER BY ordinal DESC LIMIT %s',
                          (conversation_id, before or 2147483647, limit))

    def get_turn(self, turn_id: str) -> dict | None:
        rows = self._rows('SELECT * FROM rag_conversation_turns WHERE id=%s', (turn_id,))
        return rows[0] if rows else None

    def find_request(self, conversation_id: str, request_id) -> dict | None:
        rows = self._rows('SELECT * FROM rag_conversation_turns WHERE conversation_id=%s AND request_id=%s',(conversation_id,request_id))
        return rows[0] if rows else None

    def dependencies(self, turn_id: str) -> list[tuple[str,str,str]]:
        return self.connection.execute('SELECT meeting_id,project_id,revision FROM rag_turn_sources WHERE turn_id=%s ORDER BY meeting_id,project_id,revision',
                                       (turn_id,)).fetchall()

    def add_dependencies(self, turn_id: str, dependencies: list[tuple[str,str,str]]) -> None:
        with self.connection.transaction():
            for dependency in set(dependencies):
                self.connection.execute('INSERT INTO rag_turn_sources VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING', (turn_id,*dependency))

    def set_scope(self, conversation_id: str, project_id: str | None) -> None:
        with self.connection.transaction():
            self.connection.execute('SELECT id FROM rag_conversations WHERE id=%s FOR UPDATE', (conversation_id,))
            if self.connection.execute('SELECT 1 FROM rag_conversation_turns WHERE conversation_id=%s LIMIT 1', (conversation_id,)).fetchone():
                raise RagError('scope_locked',409)
            self.connection.execute('UPDATE rag_conversations SET project_id=%s,updated_at=now() WHERE id=%s', (project_id,conversation_id))

    def delete(self, conversation_id: str) -> None:
        self.connection.execute('DELETE FROM rag_conversations WHERE id=%s', (conversation_id,))

    def claim(self, conversation_id: str, request: TurnRequest) -> tuple[dict,bool]:
        with self.connection.transaction():
            row = self.connection.execute('SELECT id FROM rag_conversations WHERE id=%s FOR UPDATE', (conversation_id,)).fetchone()
            if row is None:
                raise RagError('conversation_not_found',404)
            self.connection.execute("""UPDATE rag_conversation_turns SET status='failed',claim_id=NULL,lease_until=NULL,
                failure_code='turn_interrupted',finished_at=now() WHERE conversation_id=%s AND status='running' AND lease_until<now()""", (conversation_id,))
            existing = self._rows('SELECT * FROM rag_conversation_turns WHERE conversation_id=%s AND request_id=%s', (conversation_id,request.request_id))
            config = request.config.model_dump()
            if existing:
                turn = existing[0]
                if turn['question'] != request.question or ConversationConfig.model_validate(turn['config']).model_dump() != config:
                    raise RagError('idempotency_conflict',409)
                return turn,False
            if self.connection.execute("SELECT 1 FROM rag_conversation_turns WHERE conversation_id=%s AND status='running'", (conversation_id,)).fetchone():
                raise RagError('turn_generating',409)
            ordinal = self.connection.execute('SELECT coalesce(max(ordinal),0) FROM rag_conversation_turns WHERE conversation_id=%s', (conversation_id,)).fetchone()[0]
            if ordinal != request.expected_last_ordinal:
                raise RagError('conversation_changed',409)
            turn = self._rows("""INSERT INTO rag_conversation_turns(conversation_id,ordinal,request_id,question,config,status,claim_id,lease_until)
                VALUES (%s,%s,%s,%s,%s,'running',%s,now()+interval '90 seconds') RETURNING *""",
                (conversation_id,ordinal+1,request.request_id,request.question,Jsonb(config),uuid4()))[0]
            self.connection.execute('UPDATE rag_conversations SET config=%s,title=CASE WHEN %s=0 THEN %s ELSE title END,updated_at=now() WHERE id=%s',
                                    (Jsonb(config),ordinal,request.question[:100],conversation_id))
            return turn,True

    def finish(self, turn_id: str, claim_id, answer: dict, resolved_question: str | None, metadata: dict, before_commit: Callable[[], None] | None = None) -> bool:
        with self.connection.transaction():
            self.connection.execute('''SELECT c.id FROM rag_conversations c JOIN rag_conversation_turns t
                ON t.conversation_id=c.id WHERE t.id=%s FOR UPDATE OF c''', (turn_id,))
            row = self.connection.execute("""UPDATE rag_conversation_turns SET status='completed',claim_id=NULL,lease_until=NULL,
                answer=%s,resolved_question=%s,metadata=%s,finished_at=now() WHERE id=%s AND claim_id=%s
                AND status='running' AND lease_until>now() RETURNING conversation_id""",
                (Jsonb(answer),resolved_question,Jsonb(metadata),turn_id,claim_id)).fetchone()
            if row:
                self.connection.execute('UPDATE rag_conversations SET updated_at=now() WHERE id=%s', (row[0],))
                if before_commit is not None:
                    before_commit()
            return row is not None

    def fail(self, turn_id: str, claim_id, code: str) -> None:
        self.connection.execute("""UPDATE rag_conversation_turns SET status='failed',claim_id=NULL,lease_until=NULL,
            failure_code=%s,answer=NULL,finished_at=now() WHERE id=%s AND claim_id=%s AND status='running'""", (code,turn_id,claim_id))
