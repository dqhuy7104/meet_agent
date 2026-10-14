"""Unit tests for VietSuperSpeech manifest loading and language routing."""

import argparse
import io
import json
import unittest
import wave
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import numpy as np

from ai_training.evaluation.evaluate_vietsuperspeech_silero_phowhisper import (
    ManifestSample,
    PreparedSample,
    load_manifest_samples,
    main,
    prepare_speech_regions,
    transcribe_prepared_samples,
)


class VietSuperSpeechManifestTest(unittest.TestCase):
    """Verify manifest validation and audio-path handling."""

    def make_dataset(self, entries: object, split: str = "dev") -> Path:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        audio = root / "audio" / "sample.wav"
        audio.parent.mkdir()
        audio.touch()
        (root / f"{split}.json").write_text(json.dumps(entries), encoding="utf-8")
        return root

    def test_loads_references_in_manifest_order_and_applies_limit(self) -> None:
        root = self.make_dataset(
            [
                {"audio": "audio/sample.wav", "text": "câu đầu"},
                {"audio": "audio/sample.wav", "text": "câu sau"},
            ]
        )

        samples = load_manifest_samples(root, "dev", limit=1)

        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0].reference, "câu đầu")
        self.assertEqual(samples[0].audio_path, (root / "audio" / "sample.wav").resolve())

    def test_rejects_audio_path_outside_dataset_root(self) -> None:
        root = self.make_dataset([{"audio": "../outside.wav", "text": "câu nói"}])

        with self.assertRaisesRegex(ValueError, "escapes dataset root"):
            load_manifest_samples(root, "dev", limit=None)

    def test_rejects_missing_audio_and_empty_text(self) -> None:
        root = self.make_dataset([{"audio": "audio/missing.wav", "text": ""}])

        with self.assertRaisesRegex(ValueError, "non-empty string"):
            load_manifest_samples(root, "dev", limit=None)

    def test_rejects_a_manifest_that_is_not_an_array(self) -> None:
        root = self.make_dataset({"audio": "audio/sample.wav", "text": "câu nói"})

        with self.assertRaisesRegex(ValueError, "JSON array"):
            load_manifest_samples(root, "dev", limit=None)


class VietSuperSpeechRoutingTest(unittest.TestCase):
    def test_report_records_flow_and_split_fingerprint(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            audio_path = root / "sample.wav"
            audio_path.touch()
            (root / "dev.json").write_text(
                json.dumps([{"audio": "sample.wav", "text": "xin chào"}]), encoding="utf-8"
            )
            output = root / "report.json"
            args = argparse.Namespace(
                dataset_root=root,
                split="dev",
                limit=None,
                model=None,
                whisper_model=None,
                language_model=None,
                device="cpu",
                output=output,
            )
            prepared = PreparedSample(audio_path, "xin chào", 1.0, 0.0, 16_000, [])
            with patch(
                "ai_training.evaluation.evaluate_vietsuperspeech_silero_phowhisper.parse_args", return_value=args
            ), patch(
                "ai_training.evaluation.evaluate_vietsuperspeech_silero_phowhisper.prepare_speech_regions",
                return_value=[prepared],
            ):
                main()
            report = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(report["split"], "dev")
        self.assertEqual(report["sample_count"], 1)
        self.assertEqual(len(report["manifest_sha256"]), 64)
        self.assertEqual(report["mean_wer"], 1.0)
        self.assertEqual(report["samples"][0]["asr_routes"], [])
        self.assertIsNone(report["language_provider"])

    def test_vad_regions_remain_separate(self) -> None:
        with TemporaryDirectory() as directory:
            audio_path = Path(directory) / "sample.wav"
            with io.BytesIO() as buffer:
                with wave.open(buffer, "wb") as writer:
                    writer.setnchannels(1)
                    writer.setsampwidth(2)
                    writer.setframerate(16_000)
                    writer.writeframes(b"\x00\x00" * 32_000)
                audio_path.write_bytes(buffer.getvalue())
            models = Mock()
            with patch(
                "silero_vad.get_speech_timestamps",
                return_value=[{"start": 0, "end": 16_000}, {"start": 16_000, "end": 32_000}],
            ):
                prepared = prepare_speech_regions([ManifestSample(audio_path, "xin chào")], models)
        self.assertEqual(len(prepared[0].speech_regions), 2)
        self.assertEqual(prepared[0].speech_seconds, 2.0)

    def test_routes_vietnamese_and_other_regions_independently(self) -> None:
        sample = PreparedSample(
            audio_path=Path("sample.wav"),
            reference="xin chào hello",
            audio_seconds=2.0,
            speech_seconds=2.0,
            sampling_rate=16_000,
            speech_regions=[np.zeros(16_000, dtype=np.float32) for _ in range(2)],
        )
        models = Mock()
        detector = Mock(side_effect=["vi", "other"])
        models.get_language.return_value = detector
        models.get_asr.return_value = Mock(return_value={"text": "xin chào"})
        models.get_whisper.return_value = Mock(return_value={"text": "hello"})

        transcriptions = transcribe_prepared_samples([sample], models)

        self.assertEqual(transcriptions[0].hypothesis, "xin chào hello")
        self.assertEqual(transcriptions[0].languages, ["vi", "other"])
        self.assertEqual(transcriptions[0].asr_routes, ["phowhisper", "whisper"])
        models.get_asr.return_value.assert_called_once()
        models.get_whisper.return_value.assert_called_once_with(
            {"array": sample.speech_regions[1], "sampling_rate": 16_000},
            generate_kwargs={"task": "transcribe"},
        )

    def test_no_speech_skips_language_and_asr(self) -> None:
        sample = PreparedSample(Path("sample.wav"), "xin chào", 1.0, 0.0, 16_000, [])
        models = Mock()

        transcriptions = transcribe_prepared_samples([sample], models)

        self.assertEqual(transcriptions[0].hypothesis, "")
        self.assertEqual(transcriptions[0].inference_seconds, 0.0)
        models.get_language.assert_not_called()
        models.get_asr.assert_not_called()
        models.get_whisper.assert_not_called()


if __name__ == "__main__":
    unittest.main()
