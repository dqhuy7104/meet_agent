"""Synthetic calibration tests; no model downloads or private audio."""
import io
import json
import unittest
from unittest.mock import patch

from ai_training.evaluation.evaluate_voice_identification import (
    ScoredProbe, choose_threshold, read_manifest, evaluate_manifest,
)


class VoiceCalibrationTests(unittest.TestCase):
    def manifest(self, text):
        return read_manifest(io.StringIO('audio_path,person_id,session_id,role\n' + text))

    def test_rejects_shared_sessions(self):
        with self.assertRaisesRegex(ValueError, 'session'):
            self.manifest('private.wav,p,s,enrollment\nother.wav,p,s,probe\nunknown.wav,u,x,probe\n')

    def test_selects_highest_correct_identification_with_safe_far(self):
        scores = [ScoredProbe(.9, .5, True, True), ScoredProbe(.75, .5, True, True),
                  ScoredProbe(.7, .6, False, False)]
        result = choose_threshold(scores, [.7, .75, .95], [0, .15])
        self.assertEqual(result.correct_identification_rate, 1)
        self.assertLessEqual(result.false_accept_rate, .01)
        self.assertIn(result.min_cosine, [.7, .75])

    def test_rejects_incomplete_manifest_row(self):
        with self.assertRaises(ValueError):
            self.manifest("a,p\n")

    def test_no_safe_candidate(self):
        self.assertIsNone(choose_threshold([ScoredProbe(.9, None, False, False),
                                           ScoredProbe(.9, None, True, True)], [.5], [0]))

    def test_wrong_known_assignment_counts_as_false_accept(self):
        scores = [ScoredProbe(.9, .1, True, False), ScoredProbe(.1, None, False, False)]
        self.assertIsNone(choose_threshold(scores, [.5], [0]))

    def test_requires_known_and_unknown_probes(self):
        with self.assertRaises(ValueError):
            self.manifest('a,p,s,enrollment\nb,p,t,probe\n')

    def test_report_private_fifo_and_production_scoring(self):
        rows = self.manifest(''.join(f'private-{i}.wav,p,s{i},enrollment\n' for i in range(6)) +
                             'known.wav,p,heldout,probe\nunknown.wav,u,other,probe\n')
        vectors = [[0, 1]] + [[1, 0]] * 6 + [[0, 1]]
        with patch('app.services.voice.enrollment.extract_upload_embedding', side_effect=vectors), \
             patch('pathlib.Path.read_bytes', return_value=b'private audio'):
            report = evaluate_manifest(rows, object(), 'fingerprint', [.8], [.1])
        self.assertEqual(report['sample_count'], 8)
        self.assertEqual(report['retained_enrollment_count'], 5)
        self.assertEqual(report['fingerprint'], 'fingerprint')
        self.assertEqual(report['calibration']['correct_identification_rate'], 1)
        self.assertIn('extraction_latency_seconds', report)
        encoded = json.dumps(report)
        for secret in ['private-', 'known.wav', 'unknown.wav', 'private audio']:
            self.assertNotIn(secret, encoded)


if __name__ == '__main__':
    unittest.main()
