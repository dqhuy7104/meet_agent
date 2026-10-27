from apps.api.tests.postgres_support import seed_project
from dataclasses import replace
import tempfile
from pathlib import Path
from unittest.mock import patch

from app.core.config import Settings
from app.services.meetings.repository import MeetingRepository
from app.services.meetings.storage import initialize_meeting_storage
from app.services.meetings.worker import process_meeting_parts
from app.services.voice.matching import VoiceMatch
from app.services.voice.repository import VoiceRepository
from apps.api.tests import test_meeting_worker as worker_tests
from apps.api.tests.postgres_support import PostgresTestCase

from apps.api.tests.worker_support import run_once_inline


_result = worker_tests._result


class VoiceMeetingTest(PostgresTestCase):
    add_meeting = worker_tests.MeetingWorkerTest.add_meeting

    def setUp(self):
        super().setUp()
        self.account, self.project = seed_project(self.connection)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.data_dir = Path(temporary.name)
        initialize_meeting_storage(self.data_dir, self.connection)
        self.repository = MeetingRepository(self.connection)
        self.settings = Settings(meeting_data_dir=self.data_dir)
        self.add_meeting()
        self.voices = VoiceRepository(self.connection)
        self.profile = self.voices.create_profile('Participant')
        self.settings = replace(self.settings, voice_identification_enabled=True,
                                voice_match_min_cosine=0.8, voice_match_min_margin=0.1)

    def process(self, vectors=None):
        def audio(data, filename, models, *, speaker_embeddings=None, embedding_errors=None):
            if speaker_embeddings is not None:
                speaker_embeddings.update(vectors or {'SPEAKER_00': [1.0, 0.0]})
            result = _result()
            if vectors and 'SPEAKER_01' in vectors:
                result.diarization_segments.append(result.diarization_segments[0].model_copy(update={'speaker': 'SPEAKER_01'}))
            return result
        with patch('app.services.meetings.worker.process_audio', side_effect=audio), patch(
                'app.services.meetings.worker.pipeline_fingerprint', return_value='v1'):
            run_once_inline(self.repository, self.data_dir, self.settings, self.database)

    def test_new_meeting_matches_known_and_unknown_speakers(self):
        self.voices.add_sample(self.profile.id, [1.0, 0.0], 'v1', 'upload')
        self.process({'SPEAKER_00': [1.0, 0.0], 'SPEAKER_01': [0.0, 1.0]})
        identities = self.voices.identities_for_meeting('m1')
        self.assertEqual([identity.profile_id for identity in identities], [self.profile.id, None])
        self.assertEqual(self.repository.get_detail('m1').transcript_segments[0].speaker, 'SPEAKER_00')
        self.assertEqual(self.repository.get_meeting('m1').voice_identification_status, 'completed')

    def test_part_labels_keep_distinct_vectors(self):
        parts = self.repository.get_audio_files('m1')
        parts.append(replace(parts[0], ordinal=1))
        def audio(data, filename, models, *, speaker_embeddings, embedding_errors=None):
            speaker_embeddings['SPEAKER_00'] = [1.0, 0.0] if not sink else [0.0, 1.0]
            return _result()
        sink = {}
        with patch('app.services.meetings.worker.process_audio', side_effect=audio):
            process_meeting_parts(parts, self.data_dir, None, speaker_embeddings=sink)
        self.assertEqual(sink, {'SPEAKER_00': [1.0, 0.0], 'PART_2_SPEAKER_00': [0.0, 1.0]})

    def test_disabled_and_matching_error_preserve_transcript(self):
        self.settings = replace(self.settings, voice_identification_enabled=False)
        self.process()
        self.assertEqual(self.repository.get_meeting('m1').voice_identification_status, 'disabled')
        self.add_meeting('m2')
        self.settings = replace(self.settings, voice_identification_enabled=True)
        with patch('app.services.meetings.worker.match_speakers', side_effect=ValueError('private')):
            self.process()
        meeting = self.repository.get_meeting('m2')
        self.assertEqual((meeting.status, meeting.voice_identification_status, meeting.transcript),
                         ('completed', 'failed', 'Xin chào'))

    def test_malformed_embedding_extraction_preserves_transcript(self):
        self.check_embedding_output([object()], 'failed')

    def test_embedding_row_mismatch_preserves_transcript_and_fails_voice(self):
        self.check_embedding_output([], 'failed')

    def test_zero_and_nonfinite_centroids_complete_as_unknown(self):
        import numpy as np
        self.voices.add_sample(self.profile.id, [1.0, 0.0], 'v1', 'upload')
        for index, vector in enumerate(([0., 0.], [float('nan'), 1.], [float('inf'), 1.])):
            meeting_id = 'm1' if index == 0 else f'invalid-{index}'
            if index:
                self.add_meeting(meeting_id)
            with self.subTest(vector=vector):
                self.check_embedding_output(np.array([vector]), 'completed', meeting_id)

    def check_embedding_output(self, rows, voice_status, meeting_id='m1'):
        from types import SimpleNamespace
        from apps.api.tests.test_audio_pipeline import FakeRegistry, _wav_bytes
        from app.services.audio.preprocess import SAMPLE_RATE
        from app.services.meetings.storage import audio_path

        models = FakeRegistry()
        models.annotation = SimpleNamespace(
            speaker_diarization=models.annotation, speaker_embeddings=rows)
        part = self.repository.get_audio_files(meeting_id)[0]
        audio_path(self.data_dir, part.storage_key).write_bytes(_wav_bytes())
        with patch('app.services.meetings.worker.ModelRegistry', return_value=models), patch(
                'app.services.meetings.worker.pipeline_fingerprint', return_value='v1'), patch(
                'app.pipelines.meeting_pipeline.get_speech_timestamps',
                return_value=[{'start': 0, 'end': SAMPLE_RATE}]), patch.object(
                models, 'get_asr', return_value=lambda audio: {'text': 'Xin chào'}):
            run_once_inline(self.repository, self.data_dir, self.settings, self.database)
        meeting = self.repository.get_meeting(meeting_id)
        self.assertEqual((meeting.status, meeting.voice_identification_status, meeting.transcript),
                         ('completed', voice_status, 'Xin chào'))
        self.assertEqual(self.repository.get_detail(meeting_id).diarization_segments[0].speaker, 'SPEAKER_00')
        identities = self.voices.identities_for_meeting(meeting_id)
        if voice_status == 'failed':
            self.assertEqual(identities, [])
        else:
            self.assertEqual([identity.profile_id for identity in identities], [None])
        self.assertEqual(models.calls.count('diarization'), 1)

    def test_enrollment_does_not_reidentify_old_meeting(self):
        self.process()
        self.voices.add_sample(self.profile.id, [1.0, 0.0], 'v1', 'upload')
        self.assertIsNone(self.voices.identities_for_meeting('m1')[0].profile_id)

    def test_profile_deleted_after_snapshot_is_not_committed(self):
        self.repository.claim_next()
        match = VoiceMatch(self.profile.id, 1.0)
        self.voices.delete_profile(self.profile.id)
        self.repository.complete('m1', _result(), identities={'SPEAKER_00': match},
                                 voice_status='completed', voice_fingerprint='v1')
        self.assertIsNone(self.voices.identities_for_meeting('m1')[0].profile_id)
        self.assertEqual(self.repository.get_meeting('m1').status, 'completed')

    def test_cancelling_or_deleting_source_removes_samples(self):
        for transition in ('cancel', 'delete'):
            with self.subTest(transition=transition):
                self.add_meeting(transition)
                self.voices.add_sample(self.profile.id, [1.0, 0.0], 'v1', 'meeting', transition, 'SPEAKER_00')
                getattr(self.repository, transition)(transition)
                self.assertEqual(self.voices.list_samples(self.profile.id), [])

    def test_manual_correction_does_not_enroll(self):
        self.process()
        identity = self.voices.set_meeting_identity('m1', 'SPEAKER_00', self.profile.id)
        self.assertEqual((identity.profile_id, identity.display_name, identity.origin, identity.similarity),
                         (self.profile.id, 'Participant', 'manual', None))
        self.assertEqual(self.voices.list_samples(self.profile.id), [])
        cleared = self.voices.set_meeting_identity('m1', 'SPEAKER_00', None)
        self.assertIsNone(cleared.profile_id)
        self.assertIsNone(self.connection.execute('SELECT fingerprint FROM meeting_speaker_identities').fetchone()[0])

    def test_manual_correction_validates_meeting_speaker_and_profile(self):
        from app.services.meetings.repository import MeetingStateConflict
        with self.assertRaises(LookupError):
            self.voices.set_meeting_identity('missing', 'SPEAKER_00', self.profile.id)
        with self.assertRaises(MeetingStateConflict):
            self.voices.set_meeting_identity('m1', 'SPEAKER_00', self.profile.id)
        self.process()
        with self.assertRaises(MeetingStateConflict):
            self.voices.set_meeting_identity('m1', 'missing', self.profile.id)
        with self.assertRaises(LookupError):
            self.voices.set_meeting_identity('m1', 'SPEAKER_00', 'missing')
        self.assertIsNone(self.voices.identities_for_meeting('m1')[0].profile_id)

    def test_retry_and_failure_clear_identities_and_status(self):
        self.process()
        self.connection.execute("UPDATE meetings SET status='processing' WHERE id='m1'")
        self.repository.fail('m1', 'processing_error')
        self.assertEqual(self.voices.identities_for_meeting('m1'), [])
        self.assertEqual(self.repository.get_meeting('m1').voice_identification_status, 'not_run')
        self.repository.retry('m1')
        self.assertEqual(self.voices.identities_for_meeting('m1'), [])

    def test_purge_meeting_source_removes_identities_samples_and_pending_jobs(self):
        self.process()
        self.voices.add_sample(self.profile.id, [1.0, 0.0], 'v1', 'meeting', 'm1', 'SPEAKER_00')
        job = self.voices.enqueue_from_meeting(self.profile.id, 'm1', 'SPEAKER_00')
        self.voices.purge_meeting_source('m1')
        self.assertEqual(self.voices.list_samples(self.profile.id), [])
        self.assertEqual(self.voices.identities_for_meeting('m1'), [])
        self.assertEqual(self.voices.get_job(job.id).failure_code, 'source_changed')
