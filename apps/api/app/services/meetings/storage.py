"""Private local audio storage for meeting uploads."""

from __future__ import annotations

import os
import re
import sqlite3
import uuid
from pathlib import Path


_STORAGE_KEY = re.compile(r"[0-9a-f]{32}\Z")


def audio_path(data_dir: Path, storage_key: str) -> Path:
    """Resolve a generated storage key, never a client filename."""
    if not _STORAGE_KEY.fullmatch(storage_key):
        raise ValueError("Invalid audio storage key")
    return data_dir / "audio" / storage_key


def publish_audio(data_dir: Path, data: bytes) -> str:
    """Atomically publish upload bytes under an opaque private key."""
    storage_key = uuid.uuid4().hex
    temporary = data_dir / "tmp" / uuid.uuid4().hex
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, audio_path(data_dir, storage_key))
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return storage_key


def cleanup_orphaned_audio(data_dir: Path) -> None:
    """Remove files from interrupted uploads, retaining referenced audio."""
    for temporary in (data_dir / "tmp").iterdir():
        if temporary.is_file() or temporary.is_symlink():
            temporary.unlink()
    connection = sqlite3.connect(data_dir / "meetings.sqlite3")
    try:
        referenced = {row[0] for row in connection.execute("SELECT storage_key FROM meetings")}
    finally:
        connection.close()
    for audio in (data_dir / "audio").iterdir():
        if audio.name not in referenced and (audio.is_file() or audio.is_symlink()):
            audio.unlink()
