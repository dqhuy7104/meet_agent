"""Unit tests for VietSuperSpeech manifest loading without ML dependencies."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from ai_training.evaluation.evaluate_vietsuperspeech_silero_phowhisper import load_manifest_samples


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


if __name__ == "__main__":
    unittest.main()
