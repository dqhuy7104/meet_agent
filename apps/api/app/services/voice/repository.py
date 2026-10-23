"""Voice profile and sample persistence; public records contain metadata only."""

from dataclasses import dataclass
from datetime import datetime

import psycopg
from psycopg.rows import class_row


@dataclass(frozen=True)
class VoiceProfile:
    id: str
    display_name: str
    created_at: datetime
    updated_at: datetime
    sample_count: int


@dataclass(frozen=True)
class VoiceSample:
    id: int
    profile_id: str
    dimensions: int
    fingerprint: str
    source: str
    source_meeting_id: str | None
    source_speaker: str | None
    created_at: datetime


_PROFILE_COLUMNS = """p.id, p.display_name, p.created_at, p.updated_at,
    (SELECT count(*) FROM voice_samples s WHERE s.profile_id=p.id) AS sample_count"""
_SAMPLE_COLUMNS = """id, profile_id, dimensions, fingerprint, source,
    source_meeting_id, source_speaker, created_at"""


class VoiceRepository:
    def __init__(self, connection: psycopg.Connection) -> None:
        self.connection = connection

    def create_profile(self, display_name: str) -> VoiceProfile:
        with self.connection.transaction():
            profile_id = self.connection.execute(
                "INSERT INTO voice_profiles(display_name) VALUES (%s) RETURNING id",
                (display_name,)).fetchone()[0]
            return self._get_profile(profile_id)

    def _get_profile(self, profile_id: str) -> VoiceProfile | None:
        with self.connection.cursor(row_factory=class_row(VoiceProfile)) as cursor:
            return cursor.execute(
                f"SELECT {_PROFILE_COLUMNS} FROM voice_profiles p WHERE p.id=%s",
                (profile_id,)).fetchone()

    def list_profiles(self) -> list[VoiceProfile]:
        with self.connection.cursor(row_factory=class_row(VoiceProfile)) as cursor:
            return cursor.execute(
                f"SELECT {_PROFILE_COLUMNS} FROM voice_profiles p ORDER BY p.created_at,p.id").fetchall()

    def rename_profile(self, profile_id: str, display_name: str) -> VoiceProfile | None:
        with self.connection.transaction():
            self.connection.execute(
                "UPDATE voice_profiles SET display_name=%s,updated_at=now() WHERE id=%s",
                (display_name, profile_id))
            return self._get_profile(profile_id)

    def delete_profile(self, profile_id: str) -> tuple[str, ...] | None:
        with self.connection.transaction():
            if self.connection.execute(
                "SELECT id FROM voice_profiles WHERE id=%s FOR UPDATE", (profile_id,)).fetchone() is None:
                return None
            keys = self.connection.execute(
                "SELECT upload_key FROM voice_enrollment_jobs WHERE profile_id=%s AND upload_key IS NOT NULL",
                (profile_id,)).fetchall()
            self.connection.execute("DELETE FROM voice_profiles WHERE id=%s", (profile_id,))
            return tuple(row[0] for row in keys)

    def list_samples(self, profile_id: str) -> list[VoiceSample]:
        with self.connection.cursor(row_factory=class_row(VoiceSample)) as cursor:
            return cursor.execute(
                f"SELECT {_SAMPLE_COLUMNS} FROM voice_samples WHERE profile_id=%s ORDER BY id",
                (profile_id,)).fetchall()

    def add_sample(
        self, profile_id: str, embedding: list[float], fingerprint: str, source: str,
        source_meeting_id: str | None = None, source_speaker: str | None = None,
    ) -> VoiceSample:
        with self.connection.transaction():
            self.connection.execute("SELECT id FROM voice_profiles WHERE id=%s FOR UPDATE", (profile_id,))
            with self.connection.cursor(row_factory=class_row(VoiceSample)) as cursor:
                sample = cursor.execute(
                    f"""INSERT INTO voice_samples
                        (profile_id,embedding,dimensions,fingerprint,source,source_meeting_id,source_speaker)
                        VALUES (%s,%s::vector,%s,%s,%s,%s,%s) RETURNING {_SAMPLE_COLUMNS}""",
                    (profile_id, str(embedding), len(embedding), fingerprint, source,
                     source_meeting_id, source_speaker)).fetchone()
            self.connection.execute(
                """DELETE FROM voice_samples WHERE profile_id=%s AND id IN
                   (SELECT id FROM voice_samples WHERE profile_id=%s ORDER BY id DESC OFFSET 5)""",
                (profile_id, profile_id))
            return sample

    def delete_sample(self, profile_id: str, sample_id: int) -> bool:
        return self.connection.execute(
            "DELETE FROM voice_samples WHERE profile_id=%s AND id=%s",
            (profile_id, sample_id)).rowcount > 0
