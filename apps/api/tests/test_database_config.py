"""Database configuration stays independent of stateless audio inference."""
import unittest
from unittest.mock import patch

from app.core.database_config import DatabaseUnavailableError, load_database_settings
from app.core.rag_config import load_rag_settings


class DatabaseConfigTest(unittest.TestCase):
    def test_missing_or_legacy_url_is_rejected_without_secret(self) -> None:
        for values in ({}, {"RAG_DATABASE_URL": "postgresql://secret@localhost/old"},
                       {"DATABASE_URL": "sqlite:///private"}, {"DATABASE_URL": "invalid secret"}):
            with self.subTest(values=values), self.assertRaises(DatabaseUnavailableError) as caught:
                load_database_settings(values)
            self.assertNotIn("secret", str(caught.exception))

    def test_valid_conninfo_is_redacted_and_does_not_connect(self) -> None:
        with patch("psycopg.connect", side_effect=AssertionError("must not connect")):
            for value in ("postgresql://user:private-password@localhost/meetings",
                          "host=localhost dbname=meetings password=private-password"):
                settings = load_database_settings({"DATABASE_URL": value})
                self.assertEqual(settings.database_url.get_secret_value(), value)
                self.assertNotIn("private-password", repr(settings))

    def test_rag_settings_do_not_require_database_configuration(self) -> None:
        self.assertFalse(load_rag_settings({}).enabled)
        self.assertTrue(load_rag_settings({"RAG_ENABLED": "true"}).enabled)
