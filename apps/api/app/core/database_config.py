"""Shared PostgreSQL configuration, loaded only by storage consumers."""
from dataclasses import dataclass, field
from functools import lru_cache
import os
from typing import Mapping

from psycopg import ProgrammingError
from psycopg.conninfo import conninfo_to_dict
from pydantic import SecretStr


class DatabaseUnavailableError(RuntimeError):
    """A safe storage failure without connection details or meeting content."""

    def __init__(self, code: str = "database_unavailable") -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class DatabaseSettings:
    database_url: SecretStr = field(repr=False)


def load_database_settings(environment: Mapping[str, str]) -> DatabaseSettings:
    value = environment.get("DATABASE_URL", "").strip()
    if not value:
        raise DatabaseUnavailableError("database_not_configured")
    try:
        conninfo_to_dict(value)
    except ProgrammingError:
        raise DatabaseUnavailableError("invalid_database_configuration") from None
    return DatabaseSettings(SecretStr(value))


@lru_cache
def get_database_settings() -> DatabaseSettings:
    return load_database_settings(os.environ)
