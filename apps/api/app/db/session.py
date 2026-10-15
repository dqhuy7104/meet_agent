"""SQLite setup for the local meeting library."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path


def open_meeting_db(data_dir: Path) -> sqlite3.Connection:
    """Open one configured connection to the local meeting database."""
    connection = sqlite3.connect(data_dir / "meetings.sqlite3", timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    return connection


def initialize_meeting_store(data_dir: Path) -> None:
    """Create private storage directories and the SQLite schema."""
    for directory in (data_dir, data_dir / "audio", data_dir / "tmp"):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
    connection = open_meeting_db(data_dir)
    try:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS meetings (
                id TEXT PRIMARY KEY,
                title TEXT,
                original_filename TEXT NOT NULL,
                storage_key TEXT NOT NULL UNIQUE,
                checksum TEXT NOT NULL,
                idempotency_key TEXT UNIQUE,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('queued', 'processing', 'completed', 'failed')),
                attempt_count INTEGER NOT NULL DEFAULT 0,
                duration_seconds REAL,
                failure_code TEXT,
                transcript TEXT
            );
            CREATE TABLE IF NOT EXISTS vad_segments (
                meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
                ordinal INTEGER NOT NULL,
                start_seconds REAL NOT NULL,
                end_seconds REAL NOT NULL,
                PRIMARY KEY (meeting_id, ordinal)
            );
            CREATE TABLE IF NOT EXISTS diarization_segments (
                meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
                ordinal INTEGER NOT NULL,
                start_seconds REAL NOT NULL,
                end_seconds REAL NOT NULL,
                speaker TEXT NOT NULL,
                PRIMARY KEY (meeting_id, ordinal)
            );
            CREATE TABLE IF NOT EXISTS transcript_segments (
                id INTEGER PRIMARY KEY,
                meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
                ordinal INTEGER NOT NULL,
                start_seconds REAL NOT NULL,
                end_seconds REAL NOT NULL,
                speaker TEXT NOT NULL,
                text TEXT NOT NULL,
                UNIQUE (meeting_id, ordinal)
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS transcript_fts USING fts5(
                meeting_id UNINDEXED, segment_id UNINDEXED, text, tokenize='unicode61'
            );
            CREATE INDEX IF NOT EXISTS meetings_created ON meetings(created_at DESC, id DESC);
            """
        )
        connection.commit()
    finally:
        connection.close()
    os.chmod(data_dir / "meetings.sqlite3", 0o600)
