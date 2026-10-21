"""Apply shared SQL migrations explicitly before starting API or workers."""
import hashlib
from pathlib import Path

import psycopg

from app.core.database_config import DatabaseUnavailableError, get_database_settings
from app.db.session import open_database

MIGRATION_LOCK = (0x4D454554, 0)


def migration_definitions() -> list[tuple[int, str, str]]:
    return [(int(path.name.split('_')[0]), hashlib.sha256(path.read_bytes()).hexdigest(), path.read_text())
            for path in sorted((Path(__file__).parent / "migrations").glob("*.sql"))]


def migrate_database(connection: psycopg.Connection) -> None:
    with connection.transaction():
        connection.execute("SELECT pg_advisory_xact_lock(%s,%s)", MIGRATION_LOCK)
        ledger = connection.execute("SELECT to_regclass('schema_migrations')").fetchone()[0]
        if ledger is None:
            existing = connection.execute("""SELECT tablename FROM pg_tables WHERE schemaname=current_schema()
                AND (tablename IN ('meetings','bot_sessions','storage_identity') OR tablename LIKE 'rag_%%')""").fetchall()
            if existing:
                raise DatabaseUnavailableError("unversioned_database")
            connection.execute("""CREATE TABLE schema_migrations (
                version integer PRIMARY KEY, checksum text NOT NULL,
                applied_at timestamptz NOT NULL DEFAULT now())""")
        applied = dict(connection.execute("SELECT version,checksum FROM schema_migrations").fetchall())
        definitions = migration_definitions()
        expected = {version: checksum for version, checksum, _ in definitions}
        if any(expected.get(version) != checksum for version, checksum in applied.items()):
            raise DatabaseUnavailableError("database_schema_incompatible")
        for version, checksum, content in definitions:
            if version not in applied:
                connection.execute(content)
                connection.execute("INSERT INTO schema_migrations(version,checksum) VALUES (%s,%s)", (version, checksum))


def main() -> None:
    try:
        with open_database(get_database_settings()) as connection:
            migrate_database(connection)
    except (DatabaseUnavailableError, psycopg.Error):
        raise SystemExit("Database migration unavailable; check configuration, schema and extension permissions") from None


if __name__ == "__main__":
    main()
