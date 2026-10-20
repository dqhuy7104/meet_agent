import unittest
from dataclasses import replace
from app.services.rag.chunking import chunk_transcript, make_snapshot, estimate_tokens
from app.services.rag.types import SourceSegment


class ChunkingTest(unittest.TestCase):
    def test_revision_and_boundaries(self):
        segments = (SourceSegment(0, 'Xin chào cuộc họp', 'A', 1, 8), SourceSegment(1, 'Kế hoạch mới', 'B', 5, 12))
        snapshot = make_snapshot('m', 'Title', segments)
        chunks = chunk_transcript(snapshot)
        self.assertEqual(chunks, chunk_transcript(snapshot))
        self.assertEqual(chunks[0].start_seconds, 1)
        self.assertEqual(chunks[0].end_seconds, 12)
        self.assertEqual(chunks[0].speaker, 'A, B')
        self.assertEqual(chunks[0].source_segments, segments)
        self.assertNotEqual(snapshot.revision, make_snapshot('m', 'Title', (replace(segments[0], text='hai từ'),)).revision)
        self.assertNotEqual(snapshot.revision, make_snapshot('m', 'Renamed', segments).revision)
        self.assertEqual(chunk_transcript(make_snapshot('m', 'Title', ())), [])

    def test_unicode_splitting_and_progress(self):
        text = 'Tiếng Việt 日本語 rất tốt. ' * 120
        snapshot = make_snapshot('m', 'Title', (SourceSegment(0, text, 'A', 3, 100),))
        chunks = chunk_transcript(snapshot, target_tokens=30, overlap_tokens=0)
        self.assertEqual(''.join(c.text for c in chunks), text)
        self.assertTrue(all(estimate_tokens(c.text) <= 30 for c in chunks))
        self.assertTrue(all(c.start_seconds == 3 and c.end_seconds == 100 for c in chunks))
        overlap = chunk_transcript(snapshot, target_tokens=30, overlap_tokens=5)
        self.assertGreater(len(overlap), len(chunks))
        self.assertEqual(overlap, chunk_transcript(snapshot, 30, 5))
        with self.assertRaises(ValueError):
            chunk_transcript(snapshot, 30, 30)
