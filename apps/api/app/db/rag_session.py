"""Compatibility migration entry point delegates to the shared history."""
from psycopg import Connection
from app.db.migrate import migrate_database


def migrate_rag_db(connection: Connection) -> None:
    migrate_database(connection)
