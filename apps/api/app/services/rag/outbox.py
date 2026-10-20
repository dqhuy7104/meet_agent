"""SQLite delivery metadata; enqueue participates in the caller transaction."""
import sqlite3


def enqueue_rag_event(connection: sqlite3.Connection, meeting_id: str, kind: str) -> None:
    connection.execute('INSERT INTO rag_outbox(meeting_id,kind) VALUES (?,?)', (meeting_id, kind))


def acknowledge(connection: sqlite3.Connection, meeting_id: str, through_id: int) -> None:
    with connection:
        connection.execute('DELETE FROM rag_outbox WHERE meeting_id=? AND id<=?', (meeting_id, through_id))
