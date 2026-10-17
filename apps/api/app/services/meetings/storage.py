"""Private local audio storage for meeting uploads."""

from __future__ import annotations

import fcntl
import os
import re
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


_STORAGE_KEY = re.compile(r"[0-9a-f]{32}\Z")


@contextmanager
def audio_publication_lock(data_dir: Path) -> Iterator[None]:
    """Serialize upload publication and orphan cleanup across processes."""
    descriptor = os.open(data_dir / "publication.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


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


def publish_audio_file(data_dir: Path, temporary_path: Path) -> str:
    """Move a closed recorder file into private storage with an opaque key."""
    if temporary_path.parent.resolve() != (data_dir / "tmp").resolve() or not temporary_path.is_file():
        raise ValueError("Recorder file must be inside the private temporary directory")
    storage_key = uuid.uuid4().hex
    temporary_path.chmod(0o600)
    with temporary_path.open("rb") as source:
        os.fsync(source.fileno())
    os.replace(temporary_path, audio_path(data_dir, storage_key))
    return storage_key


def cleanup_orphaned_audio(data_dir: Path) -> None:
    """Remove files from interrupted uploads, retaining referenced audio."""
    with audio_publication_lock(data_dir):
        for temporary in (data_dir / "tmp").iterdir():
            if temporary.is_file() or temporary.is_symlink():
                temporary.unlink(missing_ok=True)
        connection = sqlite3.connect(data_dir / "meetings.sqlite3")
        try:
            referenced = {row[0] for row in connection.execute("SELECT storage_key FROM meeting_audio_files")}
            referenced.update(row[0] for row in connection.execute("SELECT storage_key FROM bot_session_parts"))
        finally:
            connection.close()
        for audio in (data_dir / "audio").iterdir():
            if audio.name not in referenced and (audio.is_file() or audio.is_symlink()):
                audio.unlink(missing_ok=True)
