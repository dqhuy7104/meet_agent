import unittest
from app.services.rag.types import Candidate, SourceChunk, SourceSegment
from app.services.rag.ranking import rank_candidates, select_context


def candidate(meeting, ordinal=0, cosine=.9, text='Kế hoạch Việt Nam'):
    return Candidate(SourceChunk(f'{meeting}:{ordinal}', meeting, meeting, 'r', ordinal, text,
                                 (SourceSegment(ordinal, text, 'A', 1, 2),)), cosine)


class RankingTest(unittest.TestCase):
    def test_fusion_deduplication_and_meeting_diversity(self):
        a = [candidate('A', i) for i in range(100)]
        b, c = candidate('B'), candidate('C')
        ranked = rank_candidates('kế hoạch', a+[b, c], [b, c, a[0]])
        self.assertLessEqual(len(ranked), 40)
        self.assertEqual(len({r.chunk.source_id for r in ranked}), len(ranked))
        context = select_context(ranked, 3, 1000)
        self.assertEqual({c.meeting_id for c in context}, {'A', 'B', 'C'})
        self.assertLessEqual(sum(c.meeting_id == 'A' for c in context), 5)
        self.assertEqual(len({c.meeting_id for c in select_context(ranked, 1, 1000)}), 1)
        self.assertEqual(select_context(ranked, 3, 1), [])

    def test_unrelated_hits_and_unicode_ties(self):
        self.assertEqual(rank_candidates('ngân sách', [candidate('A', cosine=.1, text='khác')], []), [])
        hits = [candidate('B'), candidate('A')]
        ranked = rank_candidates('VIỆT, nam!', hits, [])
        self.assertEqual([r.chunk.meeting_id for r in ranked], ['A', 'B'])
        self.assertEqual(ranked, rank_candidates('việt nam', hits, []))
