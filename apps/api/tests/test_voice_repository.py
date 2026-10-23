from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from threading import Barrier

import psycopg

from app.services.meetings.repository import MeetingRepository
from app.services.voice.repository import VoiceRepository
from apps.api.tests.postgres_support import PostgresTestCase


class VoiceRepositoryTest(PostgresTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.repository = VoiceRepository(self.connection)
        self.profile = self.repository.create_profile("Participant")

    def test_sixth_successful_sample_evicts_oldest(self) -> None:
        inserted_ids = [self.repository.add_sample(self.profile.id, [1.0, 0.0], "v1", "upload").id
                        for _ in range(6)]
        self.assertEqual([s.id for s in self.repository.list_samples(self.profile.id)], inserted_ids[1:])

    def test_concurrent_insert_keeps_latest_five(self) -> None:
        for _ in range(4):
            self.repository.add_sample(self.profile.id, [1.0, 0.0], "v1", "upload")
        barrier = Barrier(2)

        def insert() -> int:
            with self.open_connection() as connection:
                barrier.wait(timeout=5)
                return VoiceRepository(connection).add_sample(self.profile.id, [0.0, 1.0], "v1", "upload").id

        with ThreadPoolExecutor(max_workers=2) as executor:
            ids = list(executor.map(lambda _: insert(), range(2)))
        samples = self.repository.list_samples(self.profile.id)
        self.assertEqual(len(samples), 5)
        self.assertEqual([s.id for s in samples][-2:], sorted(ids))

    def test_failed_insert_does_not_evict(self) -> None:
        original_ids = [self.repository.add_sample(self.profile.id, [1.0, 0.0], "v1", "upload").id
                        for _ in range(5)]
        with self.assertRaises(psycopg.errors.CheckViolation):
            self.repository.add_sample(self.profile.id, [1.0, 0.0], "v1", "invalid")
        self.assertEqual([s.id for s in self.repository.list_samples(self.profile.id)], original_ids)

    def test_delete_profile_removes_samples_and_clears_names(self) -> None:
        MeetingRepository(self.connection).create_meeting("m", None, "a.wav", "a" * 32, "hash", None)
        self.repository.add_sample(self.profile.id, [1.0, 0.0], "v1", "upload")
        self.connection.execute(
            "INSERT INTO meeting_speaker_identities(meeting_id,speaker,profile_id,origin) VALUES ('m','S0',%s,'manual')",
            (self.profile.id,))
        self.assertEqual(self.repository.delete_profile(self.profile.id), ())
        self.assertEqual(self.repository.list_samples(self.profile.id), [])
        self.assertIsNone(self.connection.execute(
            "SELECT profile_id FROM meeting_speaker_identities WHERE meeting_id='m'").fetchone()[0])
        self.assertIsNone(self.repository.delete_profile(self.profile.id))

    def test_metadata_crud_and_scoped_sample_deletion(self) -> None:
        renamed = self.repository.rename_profile(self.profile.id, "Renamed")
        self.assertEqual(renamed.display_name, "Renamed")
        sample = self.repository.add_sample(self.profile.id, [1.0, 0.0], "v1", "upload")
        self.assertEqual(self.repository.list_profiles()[0].sample_count, 1)
        self.assertNotIn("embedding", asdict(sample))
        other = self.repository.create_profile("Other")
        self.assertFalse(self.repository.delete_sample(other.id, sample.id))
        self.assertTrue(self.repository.delete_sample(self.profile.id, sample.id))
        self.assertFalse(self.repository.delete_sample(self.profile.id, sample.id))
        self.assertIsNone(self.repository.rename_profile("missing", "Name"))

    def test_source_meeting_deletion_removes_sample(self) -> None:
        MeetingRepository(self.connection).create_meeting("m", None, "a.wav", "a" * 32, "hash", None)
        self.repository.add_sample(self.profile.id, [1.0, 0.0], "v1", "meeting", "m", "S0")
        self.connection.execute("DELETE FROM meetings WHERE id='m'")
        self.assertEqual(self.repository.list_samples(self.profile.id), [])
