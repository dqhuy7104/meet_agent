"""Publish uploaded audio and PostgreSQL metadata as one blocking request unit."""
import hashlib
from pathlib import Path
import uuid

import psycopg

from app.core.database_config import DatabaseSettings
from app.db.session import open_database, validate_database
from app.services.meetings.repository import IdempotencyConflict, MeetingRecord, MeetingRepository
from app.services.meetings.storage import audio_path, audio_publication_lock, initialize_meeting_storage, publish_audio


def ingest_meeting(database: DatabaseSettings, data_dir: Path, parts: list[tuple[str, bytes]],
                   title: str | None, idempotency_key: str | None, *,
                   project_id: str, uploaded_by_account_id: str) -> MeetingRecord:
    if len(parts) == 1:
        checksum = hashlib.sha256(parts[0][1]).hexdigest()
    else:
        digest = hashlib.sha256()
        for _, data in parts:
            digest.update(len(data).to_bytes(8, 'big'))
            digest.update(data)
        checksum = digest.hexdigest()
    with open_database(database) as connection:
        validate_database(connection)
        initialize_meeting_storage(data_dir, connection)
        repository = MeetingRepository(connection)
        if idempotency_key:
            existing = repository.find_by_idempotency_key(idempotency_key)
            if existing:
                if (existing.checksum != checksum or existing.project_id != project_id
                        or existing.uploaded_by_account_id != uploaded_by_account_id):
                    raise IdempotencyConflict(idempotency_key)
                return existing
        with audio_publication_lock(data_dir):
            published: list[tuple[str, str]] = []
            meeting_id = uuid.uuid4().hex
            try:
                for filename, data in parts:
                    published.append((filename, publish_audio(data_dir, data)))
                return repository.create_meeting(meeting_id, title, parts[0][0], published[0][1],
                    checksum, idempotency_key, audio_files=published,
                    project_id=project_id, uploaded_by_account_id=uploaded_by_account_id)
            except (psycopg.OperationalError, psycopg.InterfaceError):
                # The server may have committed before its acknowledgement was lost.
                with open_database(database) as recovery:
                    record = MeetingRepository(recovery).get_meeting(meeting_id)
                    if record:
                        return record
                raise
            except IdempotencyConflict:
                for _, key in published:
                    audio_path(data_dir, key).unlink(missing_ok=True)
                existing = repository.find_by_idempotency_key(idempotency_key)
                if (existing and existing.checksum == checksum and existing.project_id == project_id
                        and existing.uploaded_by_account_id == uploaded_by_account_id):
                    return existing
                raise
            except (psycopg.Error, OSError):
                for _, key in published:
                    audio_path(data_dir, key).unlink(missing_ok=True)
                raise
