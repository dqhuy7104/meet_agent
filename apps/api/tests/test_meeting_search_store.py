from app.services.meetings.repository import MeetingRepository
from apps.api.tests.postgres_support import PostgresTestCase
from apps.api.tests.test_meeting_concurrency import result


class MeetingSearchStoreTest(PostgresTestCase):
    def test_vietnamese_plain_query_and_case(self):
        repository = MeetingRepository(self.connection)
        repository.create_meeting('m', None, 'a.wav', 'a'*32, 'hash', None)
        repository.claim_next()
        repository.complete('m', result())
        for query in ('Kế hoạch', 'KẾ HOẠCH'):
            hits, count = repository.search_segments(query, 20, 0)
            self.assertEqual(count, 1)
            self.assertEqual(hits[0].text, 'Kế hoạch quý mới')
        for query in ('ke hoach', '*', '" OR *', 'nonexistent-word'):
            self.assertEqual(repository.search_segments(query, 20, 0), ([], 0))
        repository.cancel('m')
        self.assertEqual(repository.search_segments('Kế hoạch', 20, 0), ([], 0))
