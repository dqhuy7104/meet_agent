"""Explicit PostgreSQL setup, independent of audio runtime startup."""
from pathlib import Path
import psycopg
from app.core.rag_config import RagSettings


def open_rag_db(settings: RagSettings) -> psycopg.Connection:
    return psycopg.connect(settings.database_url.get_secret_value(), autocommit=True, connect_timeout=5,
                           options='-c statement_timeout=10000 -c lock_timeout=5000')


def migrate_rag_db(connection: psycopg.Connection) -> None:
    with connection.transaction():
        connection.execute((Path(__file__).parent / 'rag_migrations/001_initial.sql').read_text())
