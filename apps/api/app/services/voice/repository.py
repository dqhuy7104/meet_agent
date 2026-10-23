"""Voice profile and sample persistence; public records contain metadata only."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

import psycopg
from psycopg.rows import class_row


@dataclass(frozen=True)
class SpeakerIdentity:
    speaker: str
    profile_id: str | None
    display_name: str | None
    similarity: float | None
    origin: str


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


@dataclass(frozen=True)
class EnrollmentJob:
    id: str
    profile_id: str | None
    source: str
    storage_key: str | None
    source_meeting_id: str | None
    source_speaker: str | None
    status: str
    attempt_count: int
    failure_code: str | None


_JOB_COLUMNS = """id, profile_id, source, upload_key AS storage_key, source_meeting_id,
    source_speaker, state AS status, attempt_count, failure_code"""


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

    def profile_embeddings(self, fingerprint: str) -> dict[str, list[list[float]]]:
        rows = self.connection.execute(
            "SELECT profile_id,embedding::text FROM voice_samples WHERE fingerprint=%s ORDER BY id",
            (fingerprint,)).fetchall()
        profiles: dict[str, list[list[float]]] = {}
        for profile_id, embedding in rows:
            profiles.setdefault(profile_id, []).append(json.loads(embedding))
        return profiles

    def enqueue_upload(self, profile_id: str, storage_key: str) -> EnrollmentJob:
        with self.connection.transaction():
            job_id = self.connection.execute(
                "INSERT INTO voice_enrollment_jobs(profile_id,source,upload_key) VALUES (%s,'upload',%s) RETURNING id",
                (profile_id, storage_key)).fetchone()[0]
            return self.get_job(job_id)

    def enqueue_from_meeting(self, profile_id: str, meeting_id: str, speaker: str) -> EnrollmentJob:
        from app.services.voice.enrollment import EnrollmentError
        with self.connection.transaction():
            meeting = self.connection.execute(
                "SELECT status FROM meetings WHERE id=%s FOR UPDATE", (meeting_id,)).fetchone()
            if meeting is None or meeting[0] != 'completed' or not self.connection.execute(
                "SELECT 1 FROM diarization_segments WHERE meeting_id=%s AND speaker=%s",
                (meeting_id, speaker)).fetchone():
                raise EnrollmentError('source_changed')
            job_id = self.connection.execute(
                """INSERT INTO voice_enrollment_jobs(profile_id,source,source_meeting_id,source_speaker)
                   VALUES (%s,'meeting',%s,%s) RETURNING id""", (profile_id, meeting_id, speaker)).fetchone()[0]
            return self.get_job(job_id)

    def get_job(self, job_id: str) -> EnrollmentJob | None:
        with self.connection.cursor(row_factory=class_row(EnrollmentJob)) as cursor:
            return cursor.execute(f"SELECT {_JOB_COLUMNS} FROM voice_enrollment_jobs WHERE id=%s", (job_id,)).fetchone()

    def claim_next_job(self) -> EnrollmentJob | None:
        with self.connection.transaction():
            row = self.connection.execute("""SELECT id FROM voice_enrollment_jobs WHERE state='queued'
                ORDER BY created_at,id LIMIT 1 FOR UPDATE SKIP LOCKED""").fetchone()
            if row is None:
                return None
            self.connection.execute("""UPDATE voice_enrollment_jobs SET state='processing',
                attempt_count=attempt_count+1,updated_at=now() WHERE id=%s""", (row[0],))
            return self.get_job(row[0])

    def set_job_process(self, job_id: str, pid: int, start_ticks: int) -> bool:
        return self.connection.execute("""UPDATE voice_enrollment_jobs SET pipeline_pid=%s,pipeline_start_ticks=%s
            WHERE id=%s AND state='processing'""", (pid, start_ticks, job_id)).rowcount == 1

    def complete_job(self, job_id: str, embedding: list[float], fingerprint: str) -> bool:
        with self.connection.transaction():
            job = self.get_job(job_id)
            if job is None or job.status != 'processing':
                return False
            # Parent rows precede the job lock, matching deletion/cancellation locks.
            source = None
            if job.source == 'meeting':
                source = self.connection.execute(
                    "SELECT status FROM meetings WHERE id=%s FOR UPDATE", (job.source_meeting_id,)).fetchone()
            profile = self.connection.execute(
                "SELECT id FROM voice_profiles WHERE id=%s FOR UPDATE", (job.profile_id,)).fetchone()
            self.connection.execute("SELECT id FROM voice_enrollment_jobs WHERE id=%s FOR UPDATE", (job_id,))
            job = self.get_job(job_id)
            if job is None or job.status != 'processing':
                return False
            if profile is None or (job.source == 'meeting' and (source is None or source[0] != 'completed' or not self.connection.execute(
                "SELECT 1 FROM diarization_segments WHERE meeting_id=%s AND speaker=%s",
                (job.source_meeting_id, job.source_speaker)).fetchone())):
                self.fail_job(job_id, 'source_changed')
                return False
            self.add_sample(job.profile_id, embedding, fingerprint, job.source, job.source_meeting_id, job.source_speaker)
            self.connection.execute("UPDATE voice_enrollment_jobs SET state='completed',failure_code=NULL,updated_at=now() WHERE id=%s", (job_id,))
            return True

    def fail_job(self, job_id: str, code: str) -> None:
        if code not in {'audio_too_long', 'invalid_audio', 'insufficient_speech', 'multiple_speakers',
                        'source_changed', 'model_unavailable', 'processing_error', 'interrupted'}:
            code = 'processing_error'
        self.connection.execute("""UPDATE voice_enrollment_jobs SET state='failed',failure_code=%s,updated_at=now()
            WHERE id=%s AND state IN ('queued','processing')""", (code, job_id))

    def recover_interrupted_jobs(self) -> None:
        self.connection.execute("""UPDATE voice_enrollment_jobs SET
            state=CASE WHEN attempt_count<2 THEN 'queued' ELSE 'failed' END,
            failure_code=CASE WHEN attempt_count<2 THEN NULL ELSE 'interrupted' END,
            pipeline_pid=NULL,pipeline_start_ticks=NULL,updated_at=now() WHERE state='processing'""")

    def identities_for_meeting(self, meeting_id: str) -> list[SpeakerIdentity]:
        with self.connection.cursor(row_factory=class_row(SpeakerIdentity)) as cursor:
            return cursor.execute("""SELECT i.speaker,i.profile_id,p.display_name,i.score AS similarity,i.origin
                FROM meeting_speaker_identities i LEFT JOIN voice_profiles p ON p.id=i.profile_id
                WHERE i.meeting_id=%s ORDER BY i.speaker""", (meeting_id,)).fetchall()

    def set_meeting_identity(self, meeting_id: str, speaker: str, profile_id: str | None) -> SpeakerIdentity:
        from app.services.meetings.repository import MeetingStateConflict
        with self.connection.transaction():
            meeting = self.connection.execute(
                "SELECT status FROM meetings WHERE id=%s FOR UPDATE", (meeting_id,)).fetchone()
            if meeting is None:
                raise LookupError("Meeting not found")
            if meeting[0] != 'completed' or not self.connection.execute(
                "SELECT 1 FROM diarization_segments WHERE meeting_id=%s AND speaker=%s",
                (meeting_id, speaker)).fetchone():
                raise MeetingStateConflict(meeting_id)
            if profile_id is not None and not self.connection.execute(
                "SELECT id FROM voice_profiles WHERE id=%s FOR KEY SHARE", (profile_id,)).fetchone():
                raise LookupError("Voice profile not found")
            self.connection.execute("""INSERT INTO meeting_speaker_identities
                (meeting_id,speaker,profile_id,origin) VALUES (%s,%s,%s,'manual')
                ON CONFLICT (meeting_id,speaker) DO UPDATE SET profile_id=EXCLUDED.profile_id,
                origin='manual',score=NULL,fingerprint=NULL""", (meeting_id, speaker, profile_id))
            return next(identity for identity in self.identities_for_meeting(meeting_id) if identity.speaker == speaker)

    def purge_meeting_source(self, meeting_id: str) -> None:
        with self.connection.transaction():
            self.connection.execute("SELECT id FROM meetings WHERE id=%s FOR UPDATE", (meeting_id,))
            self.connection.execute("DELETE FROM voice_samples WHERE source_meeting_id=%s", (meeting_id,))
            self.connection.execute("DELETE FROM meeting_speaker_identities WHERE meeting_id=%s", (meeting_id,))
            self.connection.execute("""UPDATE voice_enrollment_jobs SET state='failed',failure_code='source_changed',
                updated_at=now() WHERE source_meeting_id=%s AND state IN ('queued','processing')""", (meeting_id,))
