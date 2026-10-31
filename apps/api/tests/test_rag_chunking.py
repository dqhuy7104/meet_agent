import unittest
from dataclasses import replace
from app.services.rag.chunking import chunk_transcript, make_snapshot, estimate_tokens
from app.services.rag.types import SourceSegment


class ChunkingTest(unittest.TestCase):
    def test_turn_chunks_preserve_words_sentences_and_original_timing(self):
        text = 'Lan phụ trách triển khai. Hạn chót là thứ sáu. Ngân sách đã được duyệt. '
        snapshot = make_snapshot('m', 'Title', (SourceSegment(0, text, 'Lan', 3, 100),))
        chunks = chunk_transcript(snapshot, target_tokens=24, overlap_tokens=0, chunk_revision='turns-v2')
        self.assertEqual(''.join(c.text for c in chunks), text)
        self.assertTrue(all(c.text.rstrip().endswith('.') for c in chunks))
        self.assertTrue(all(estimate_tokens(c.text) <= 24 for c in chunks))
        self.assertTrue(all(c.start_seconds == 3 and c.end_seconds == 100 for c in chunks))
        self.assertTrue(all(c.chunk_revision == 'turns-v2' and ':turns-v2:' in c.source_id for c in chunks))
        self.assertNotEqual(chunks[0].source_id, chunk_transcript(snapshot)[0].source_id)

    def test_turn_chunks_split_long_sentences_at_words_and_handle_large_unicode_words(self):
        text = 'nội dung không có dấu câu ' * 50 + '日本語' * 90
        snapshot = make_snapshot('m', 'Title', (SourceSegment(0, text, 'A', 0, 5),))
        chunks = chunk_transcript(snapshot, 30, 0, chunk_revision='turns-v2')
        self.assertEqual(''.join(c.text for c in chunks), text)
        self.assertTrue(all(estimate_tokens(c.text) <= 30 for c in chunks))
        overlap = chunk_transcript(snapshot, 30, 10, chunk_revision='turns-v2')
        self.assertEqual(overlap, chunk_transcript(snapshot, 30, 10, chunk_revision='turns-v2'))

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
