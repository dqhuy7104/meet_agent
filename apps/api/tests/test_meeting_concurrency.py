from concurrent.futures import ThreadPoolExecutor

from app.schemas.transcript import AudioProcessResponse, TranscriptSegment
from app.services.meetings.repository import MeetingRepository, IdempotencyConflict
from app.services.rag.outbox import acknowledge
from apps.api.tests.postgres_support import PostgresTestCase


def result(text: str = "Kế hoạch quý mới") -> AudioProcessResponse:
    return AudioProcessResponse(filename="a.wav", duration_seconds=2, transcript=text,
        vad_segments=[], diarization_segments=[], transcript_segments=[
            TranscriptSegment(start=0, end=2, speaker="SPEAKER_00", text=text)])


class MeetingConcurrencyTest(PostgresTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.repository = MeetingRepository(self.connection)
        self.repository.create_meeting("m1", "Meeting", "a.wav", "a" * 32, "checksum", "key")

    def test_concurrent_claim_has_one_winner(self) -> None:
        def claim():
            with self.open_connection() as connection:
                return MeetingRepository(connection).claim_next()
        with ThreadPoolExecutor(max_workers=2) as executor:
            claims = list(executor.map(lambda _: claim(), range(2)))
        self.assertEqual(sum(record is not None for record in claims), 1)
        self.assertEqual(self.repository.get_meeting("m1").attempt_count, 1)

    def test_cancelled_meeting_cannot_complete(self) -> None:
        self.repository.claim_next()
        self.repository.cancel("m1")
        self.repository.complete("m1", result())
        self.assertEqual(self.repository.get_meeting("m1").status, "cancelled")
        self.assertEqual(self.repository.get_detail("m1").transcript_segments, [])
        self.assertEqual(self.connection.execute("SELECT count(*) FROM transcript_segments").fetchone()[0], 0)

    def test_completion_and_outbox_roll_back_together(self) -> None:
        self.repository.claim_next()
        self.connection.execute("ALTER TABLE rag_outbox ADD CHECK (kind <> 'index')")
        with self.assertRaises(Exception):
            self.repository.complete("m1", result())
        self.assertEqual(self.repository.get_meeting("m1").status, "processing")
        self.assertEqual(self.connection.execute("SELECT count(*) FROM transcript_segments").fetchone()[0], 0)

    def test_acknowledgement_keeps_new_event(self) -> None:
        self.repository.claim_next()
        self.repository.complete("m1", result())
        event_id = self.connection.execute("SELECT max(id) FROM rag_outbox").fetchone()[0]
        self.repository.cancel("m1")
        acknowledge(self.connection, "m1", event_id)
        self.assertEqual(self.connection.execute("SELECT kind FROM rag_outbox").fetchall(), [("purge",)])

    def test_idempotency_rolls_back_and_timestamps_remain_strings(self) -> None:
        with self.assertRaises(IdempotencyConflict):
            self.repository.create_meeting("m2", None, "b.wav", "b" * 32, "other", "key")
        self.assertIsNone(self.repository.get_meeting("m2"))
        self.assertTrue(self.repository.get_meeting("m1").created_at.endswith("+00:00"))
        self.assertEqual(self.repository.count_meetings(), 1)
