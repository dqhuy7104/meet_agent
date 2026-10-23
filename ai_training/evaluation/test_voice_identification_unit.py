"""Synthetic calibration tests; no model downloads or private audio."""
import io
import json
import unittest
from dataclasses import replace
from pathlib import Path
import tempfile
from unittest.mock import patch

from ai_training.evaluation.evaluate_voice_identification import (
    ScoredProbe, choose_threshold, read_manifest, evaluate_manifest, main,
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
             patch('pathlib.Path.open', side_effect=lambda *args, **kwargs: io.BytesIO(b'private audio')):
            report = evaluate_manifest(rows, object(), 'fingerprint', [.8], [.1])
        self.assertEqual(report['sample_count'], 8)
        self.assertEqual(report['retained_enrollment_count'], 5)
        self.assertEqual(report['fingerprint'], 'fingerprint')
        self.assertEqual(report['calibration']['correct_identification_rate'], 1)
        self.assertIn('extraction_latency_seconds', report)
        encoded = json.dumps(report)
        for secret in ['private-', 'known.wav', 'unknown.wav', 'private audio']:
            self.assertNotIn(secret, encoded)

    def test_cli_report_preserves_fingerprint_without_local_model_path(self):
        from app.core.config import Settings
        from app.services.voice.matching import pipeline_fingerprint

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = root / 'private-model'
            model.mkdir()
            (model / 'config.yaml').write_text('pipeline:\n  params:\n    embedding: ' + str(model / 'embedding.bin') + '\n')
            (model / 'embedding.bin').write_bytes(b'fake weights')
            manifest = root / 'manifest.csv'
            manifest.write_text('audio_path,person_id,session_id,role\na,p,s,enrollment\nb,p,t,probe\nc,u,x,probe\n')
            for name in ('a', 'b', 'c'):
                (root / name).write_bytes(b'fake audio')
            output = root / 'report.json'
            local = Settings(pyannote_model=str(model))
            remote = replace(local, pyannote_model='pyannote/speaker-diarization-community-1', pyannote_revision='a' * 40)
            for settings in (local, remote):
                with self.subTest(model=settings.pyannote_model), patch('sys.argv', [
                    'calibration', '--manifest', str(manifest), '--output', str(output),
                    '--dataset-version', 'synthetic-v1', '--thresholds', '.8', '--margins', '.1',
                ]), patch('app.core.config.Settings.from_environment', return_value=settings), patch(
                    'app.services.voice.enrollment.extract_upload_embedding', side_effect=[[1., 0.], [1., 0.], [0., 1.]]
                ):
                    main()
                    encoded = output.read_text()
                    report = json.loads(encoded)
                    self.assertNotIn(str(root), encoded)
                    self.assertEqual(report['fingerprint'], pipeline_fingerprint(settings))
                    self.assertEqual(report['model_id'], 'local-pipeline' if settings == local else settings.pyannote_model)
                    self.assertEqual(report['model_revision'], settings.pyannote_revision)
                    self.assertEqual(report['calibration']['correct_identification_rate'], 1)

    def test_oversized_audio_is_rejected_before_extraction(self):
        from ai_training.evaluation.evaluate_voice_identification import ManifestRow

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'private-audio.wav'
            with path.open('wb') as source:
                source.truncate(25 * 1024 * 1024 + 1)
            with patch('app.services.voice.enrollment.extract_upload_embedding') as extract:
                with self.assertRaisesRegex(ValueError, '^Audio exceeds 25 MiB$'):
                    evaluate_manifest([ManifestRow(path, 'p', 's', 'enrollment')], object(), 'fingerprint', [.8], [.1])
                extract.assert_not_called()


if __name__ == '__main__':
    unittest.main()
