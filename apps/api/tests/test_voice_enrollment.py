"""Enrollment uses real decoding and deterministic diarization centroids."""
import io
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
import wave

import numpy as np

from app.schemas.transcript import SpeakerTurn
from app.services.meetings.repository import MeetingAudioFile
from app.services.meetings.storage import audio_path
from app.services.voice.enrollment import EnrollmentError, extract_upload_embedding, extract_meeting_embedding


def audio(seconds=12):
    output = io.BytesIO()
    with wave.open(output, 'wb') as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16000)
        stream.writeframes(np.zeros(int(seconds * 16000), dtype='<i2').tobytes())
    return output.getvalue()


class Models:
    def __init__(self, turns):
        self.calls = 0
        self.turns = turns

    def get_diarization(self):
        def infer(payload):
            self.calls += 1
            labels = sorted({label for _, _, label in self.turns})
            annotation = SimpleNamespace(
                labels=lambda: labels,
                itertracks=lambda yield_label: iter((SimpleNamespace(start=start, end=end), None, label)
                                                   for start, end, label in self.turns),
            )
            return SimpleNamespace(speaker_diarization=annotation,
                                   speaker_embeddings=np.array([[3., 4.] if label == 'A' else [0., 2.]
                                                                for label in labels]))
        return infer


class EnrollmentTests(unittest.TestCase):
    def assert_code(self, code, callback):
        with self.assertRaises(EnrollmentError) as raised:
            callback()
        self.assertEqual(raised.exception.code, code)

    def test_upload_accepts_one_speaker_with_ten_exclusive_seconds(self):
        vector = extract_upload_embedding(audio(), Models([(0, 10, 'A')]))
        np.testing.assert_allclose(vector, [.6, .8])

    def test_upload_counts_fragmented_exclusive_speech_at_ten_second_boundary(self):
        vector = extract_upload_embedding(audio(), Models([(0, 4, 'A'), (5, 11, 'A')]))
        np.testing.assert_allclose(vector, [.6, .8])
        self.assert_code('insufficient_speech', lambda: extract_upload_embedding(
            audio(), Models([(0, 4, 'A'), (5, 10.99, 'A')])))

    def test_upload_rejects_two_speakers_short_speech_or_invalid_audio(self):
        for turns, code in [([(0, 10, 'A'), (10, 11, 'B')], 'multiple_speakers'),
                            ([(0, 9.99, 'A')], 'insufficient_speech'), ([], 'insufficient_speech')]:
            self.assert_code(code, lambda: extract_upload_embedding(audio(), Models(turns)))
        self.assert_code('invalid_audio', lambda: extract_upload_embedding(b'bad', Models([])))

    def test_upload_rejects_decoded_duration_over_120_seconds(self):
        models = Models([(0, 121, 'A')])
        self.assert_code('audio_too_long', lambda: extract_upload_embedding(audio(121), models))
        self.assertEqual(models.calls, 0)

    def test_upload_accepts_exact_duration_limit_and_rejects_invalid_centroid(self):
        np.testing.assert_allclose(extract_upload_embedding(audio(120), Models([(0, 10, 'A')])), [.6, .8])
        models = Models([(0, 10, 'A')])
        infer = models.get_diarization()
        def invalid(payload):
            result = infer(payload)
            result.speaker_embeddings[:] = np.nan
            return result
        models.get_diarization = lambda: invalid
        self.assert_code('model_unavailable', lambda: extract_upload_embedding(audio(), models))

    def test_meeting_subtracts_overlapping_stored_turns(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            audio_path(root, '22222222222222222222222222222222').parent.mkdir(parents=True)
            audio_path(root, '22222222222222222222222222222222').write_bytes(audio())
            detail = self.detail([(40, 52, 'PART_2_OLD'), (40, 43, 'PART_2_OTHER')])
            self.assert_code('insufficient_speech', lambda: extract_meeting_embedding(
                detail, 'PART_2_OLD', root, Models([(0, 12, 'A')])))

    def test_model_failure_has_safe_code(self):
        models = Models([])
        def unavailable():
            raise RuntimeError('private token')
        models.get_diarization = unavailable
        self.assert_code('model_unavailable', lambda: extract_upload_embedding(audio(), models))

    def detail(self, turns):
        return SimpleNamespace(meeting=SimpleNamespace(status='completed'),
                               audio_files=[MeetingAudioFile(0, 'one.wav', '11111111111111111111111111111111', 12, 0),
                                            MeetingAudioFile(1, 'two.wav', '22222222222222222222222222222222', 12, 40)],
                               diarization_segments=[SpeakerTurn(start=start, end=end, speaker=label)
                                                     for start, end, label in turns])

    def test_meeting_source_uses_part_offset_with_bot_gap(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            audio_path(root, '22222222222222222222222222222222').parent.mkdir(parents=True)
            audio_path(root, '22222222222222222222222222222222').write_bytes(audio())
            detail = self.detail([(40, 52, 'PART_2_OLD')])
            vector = extract_meeting_embedding(detail, 'PART_2_OLD', root, Models([(0, 12, 'B')]))
            np.testing.assert_allclose(vector, [0, 1])
            self.assertEqual(detail.diarization_segments[0].speaker, 'PART_2_OLD')

    def test_meeting_source_rejects_79_percent_overlap_close_runner_up_or_missing_audio(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            audio_path(root, '22222222222222222222222222222222').parent.mkdir(parents=True)
            audio_path(root, '22222222222222222222222222222222').write_bytes(audio(20))
            detail = self.detail([(40, 52, 'PART_2_OLD')])
            for turns in [[(0, 9.48, 'A')], [(0, 12, 'A'), (0, 11, 'B')]]:
                self.assert_code('source_changed', lambda: extract_meeting_embedding(
                    detail, 'PART_2_OLD', root, Models(turns)))
            audio_path(root, '22222222222222222222222222222222').unlink()
            self.assert_code('source_changed', lambda: extract_meeting_embedding(
                detail, 'PART_2_OLD', root, Models([])))

    def test_meeting_source_rejects_truncation_using_original_exclusive_duration(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = audio_path(root, '22222222222222222222222222222222')
            path.parent.mkdir(parents=True)
            path.write_bytes(audio(12))
            detail = self.detail([(40, 60, 'PART_2_OLD')])
            self.assert_code('source_changed', lambda: extract_meeting_embedding(
                detail, 'PART_2_OLD', root, Models([(0, 12, 'A')])))
