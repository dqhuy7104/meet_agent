"""Durable event publication participates in the meeting transaction."""
from psycopg import Connection


def enqueue_rag_event(connection: Connection, meeting_id: str, kind: str) -> None:
    connection.execute('INSERT INTO rag_outbox(meeting_id,kind) VALUES (%s,%s)', (meeting_id, kind))


def acknowledge(connection: Connection, meeting_id: str, through_id: int) -> None:
    with connection.transaction():
        connection.execute('DELETE FROM rag_outbox WHERE meeting_id=%s AND id<=%s', (meeting_id, through_id))
