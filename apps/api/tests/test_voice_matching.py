from contextlib import contextmanager
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator

import numpy as np
from pyannote.core import Annotation, Segment

from app.core.config import ConfigurationError, Settings
from app.services.diarization.pyannote import extract_speaker_embeddings
from app.services.voice.matching import match_speakers, normalize_embedding, pipeline_fingerprint
from app.services.voice.repository import VoiceRepository
from apps.api.tests.postgres_support import PostgresTestCase


@contextmanager
def _working_directory(path: Path) -> Iterator[None]:
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


class VoiceMatchingTest(unittest.TestCase):
    def test_output_rows_follow_annotation_labels(self) -> None:
        annotation = Annotation()
        annotation[Segment(0, 1)] = 'SPEAKER_01'
        annotation[Segment(1, 2)] = 'SPEAKER_00'
        rows = np.array([[1., 0.], [0., 1.]])
        vectors = extract_speaker_embeddings(SimpleNamespace(speaker_diarization=annotation, speaker_embeddings=rows))
        self.assertEqual(vectors[annotation.labels()[0]], rows[0].tolist())
        self.assertEqual(extract_speaker_embeddings(annotation), {})

    def test_zero_nan_missing_and_wrong_dimension_are_unknown(self) -> None:
        for vector in ([], [0., 0.], [float('nan'), 1.], [1., 0., 0.]):
            with self.subTest(vector=vector):
                self.assertIsNone(match_speakers({'SPEAKER_00': vector}, {'p': [[1., 0.]]}, .8, .1)['SPEAKER_00'])
        self.assertIsNone(normalize_embedding([float('inf')]))

    def test_embedding_row_count_mismatch_is_structural_error(self) -> None:
        annotation = Annotation()
        annotation[Segment(0, 1)] = 'SPEAKER_00'
        for rows in (np.empty((0, 2)), np.ones((2, 2))):
            with self.subTest(rows=len(rows)), self.assertRaisesRegex(ValueError, '^Speaker embedding row count does not match diarization labels$'):
                extract_speaker_embeddings(SimpleNamespace(speaker_diarization=annotation, speaker_embeddings=rows))

    def test_mean_threshold_and_margin(self) -> None:
        profiles = {'p': [[1., 0.], [0., 1.]], 'other': [[-1., 0.]]}
        result = match_speakers({'s': [1., 1.]}, profiles, .9, .1)['s']
        self.assertEqual(result.profile_id, 'p')
        self.assertAlmostEqual(result.similarity, 1.)
        for candidates, threshold, margin in (({}, .8, .1), ({'p': [[0., 1.]]}, .8, .1),
                                               ({'p': [[1., 0.]], 'q': [[1., .01]]}, .8, .1)):
            self.assertIsNone(match_speakers({'s': [1., 0.]}, candidates, threshold, margin)['s'])

    def test_invalid_profile_sample_does_not_hide_valid_sample(self) -> None:
        profiles = {'known': [[float('nan'), 0.], [1., 0.]], 'other': [[0., 1.]]}
        match = match_speakers({'speaker': [1., 0.]}, profiles, .8, .1)['speaker']
        self.assertEqual(match.profile_id, 'known')
        self.assertAlmostEqual(match.similarity, 1.)

    def test_fingerprint_pins_remote_revision_and_local_checkpoint(self) -> None:
        with self.assertRaises(ConfigurationError):
            pipeline_fingerprint(Settings())
        first = pipeline_fingerprint(Settings(pyannote_revision='a' * 40))
        self.assertNotEqual(first, pipeline_fingerprint(Settings(pyannote_revision='b' * 40)))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'config.yaml').write_text(f'pipeline:\n  params:\n    embedding: {root / "embedding.bin"}\n')
            (root / 'embedding.bin').write_bytes(b'one')
            settings = Settings(pyannote_model=directory)
            first = pipeline_fingerprint(settings)
            (root / 'embedding.bin').write_bytes(b'two')
            self.assertNotEqual(first, pipeline_fingerprint(settings))

    def test_plain_relative_checkpoint_uses_process_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = root / 'model'
            model.mkdir()
            config = model / 'config.yaml'
            config.write_text('pipeline:\n  params:\n    embedding: embedding.bin\n')
            (model / 'embedding.bin').write_bytes(b'config-directory-decoy')
            checkpoint = root / 'embedding.bin'
            checkpoint.write_bytes(b'loaded-checkpoint')
            with _working_directory(root):
                settings = Settings(pyannote_model=str(config))
                first = pipeline_fingerprint(settings)
                (model / 'embedding.bin').write_bytes(b'changed-decoy')
                self.assertEqual(first, pipeline_fingerprint(settings))
                checkpoint.write_bytes(b'changed-loaded-checkpoint')
                self.assertNotEqual(first, pipeline_fingerprint(settings))

    def test_model_subfolder_and_checkpoint_mapping_references(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = root / 'model'
            model.mkdir()
            embedding = model / 'embedding'
            embedding.mkdir()
            checkpoint = embedding / 'pytorch_model.bin'
            config = model / 'config.yaml'
            references = (
                '$model/embedding',
                '{checkpoint: "' + str(model) + '", subfolder: embedding}',
                '{checkpoint: "' + str(checkpoint) + '"}',
                '{checkpoint: model, subfolder: embedding}',
                '{checkpoint: model/embedding}',
                'model/embedding',
            )
            with _working_directory(root):
                for reference in references:
                    for source in (model, config):
                        with self.subTest(reference=reference, source=source):
                            config.write_text(f'pipeline:\n  params:\n    embedding: {reference}\n')
                            checkpoint.write_bytes(b'first')
                            settings = Settings(pyannote_model=str(source))
                            first = pipeline_fingerprint(settings)
                            checkpoint.write_bytes(b'second')
                            self.assertNotEqual(first, pipeline_fingerprint(settings))


class VoiceFingerprintRepositoryTest(PostgresTestCase):
    def test_revision_mismatch_excludes_samples(self) -> None:
        repository = VoiceRepository(self.connection)
        profile = repository.create_profile('Participant')
        repository.add_sample(profile.id, [1., 0.], 'old', 'upload')
        self.assertEqual(repository.profile_embeddings('new'), {})
        self.assertEqual(repository.profile_embeddings('old'), {profile.id: [[1., 0.]]})
