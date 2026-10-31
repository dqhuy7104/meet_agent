import unittest

from evaluate_source_filter import source_metrics


class SourceFilterMetricsTest(unittest.TestCase):
    def test_kept_evidence_and_removed_distractors(self):
        result = source_metrics({'lao','japan'}, {'lao','japan'}, {'lao','japan','sports'})
        self.assertEqual(result, {'evidence_recall':1.,'irrelevant_rejection_rate':1.,'precision':1.,'abstained':False})
        self.assertEqual(source_metrics({'lao','japan'}, {'lao','sports'}, {'lao','japan','sports'})['evidence_recall'], .5)

    def test_abstention_is_not_a_successful_answer(self):
        result = source_metrics({'lao'}, set(), {'lao','sports'})
        self.assertEqual(result['evidence_recall'], 0.)
        self.assertTrue(result['abstained'])
        self.assertIsNone(result['precision'])
        self.assertIsNone(source_metrics(set(), set(), {'sports'})['evidence_recall'])
