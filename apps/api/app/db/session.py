"""Shared PostgreSQL connections; schema validation never runs DDL."""
import math
import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

from app.core.database_config import DatabaseSettings, DatabaseUnavailableError


def open_database(settings: DatabaseSettings, *, timeout_seconds: float = 5) -> psycopg.Connection:
    values = conninfo_to_dict(settings.database_url.get_secret_value())
    options = values.pop("options", "")
    values['connect_timeout'] = max(1, math.ceil(timeout_seconds))
    return psycopg.connect(**values, autocommit=True,
                           options=options + " -c statement_timeout=10000 -c lock_timeout=5000")


def validate_database(connection: psycopg.Connection) -> None:
    from app.db.migrate import migration_definitions
    try:
        schema = connection.execute('SELECT current_schema()').fetchone()[0]
        if schema is None:
            raise DatabaseUnavailableError('database_schema_missing')
        rows = connection.execute(sql.SQL("SELECT version, checksum FROM {} ORDER BY version").format(
            sql.Identifier(schema, 'schema_migrations'))).fetchall()
    except psycopg.errors.UndefinedTable:
        raise DatabaseUnavailableError("database_schema_missing") from None
    if rows != [(version, checksum) for version, checksum, _ in migration_definitions()]:
        raise DatabaseUnavailableError("database_schema_incompatible")
