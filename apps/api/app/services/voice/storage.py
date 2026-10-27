"""Publish private enrollment audio under the shared audio lifecycle lock."""
from pathlib import Path

import psycopg

from app.core.database_config import DatabaseSettings
from app.db.session import open_database, validate_database
from app.services.meetings.storage import audio_path, audio_publication_lock, initialize_meeting_storage, publish_audio
from app.services.voice.repository import EnrollmentJob, VoiceRepository


def ingest_enrollment_upload(database: DatabaseSettings, data_dir: Path, profile_id: str, data: bytes,
                             created_by_account_id: str | None = None) -> EnrollmentJob:
    with open_database(database) as connection:
        validate_database(connection)
        initialize_meeting_storage(data_dir, connection)
        with audio_publication_lock(data_dir):
            key = publish_audio(data_dir, data)
            try:
                return VoiceRepository(connection).enqueue_upload(profile_id, key, created_by_account_id)
            except (psycopg.OperationalError, psycopg.InterfaceError):
                # A lost acknowledgement may follow a successful commit.
                with open_database(database) as recovery:
                    row = recovery.execute("SELECT id FROM voice_enrollment_jobs WHERE upload_key=%s", (key,)).fetchone()
                    if row:
                        return VoiceRepository(recovery).get_job(row[0])
                raise
            except (psycopg.Error, OSError):
                audio_path(data_dir, key).unlink(missing_ok=True)
                raise
