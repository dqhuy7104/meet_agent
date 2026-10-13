"""Fast tests for VietSuperSpeech training input preparation."""

from __future__ import annotations

import io
import unittest
import wave
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import numpy as np
import torch

from ai_training.evaluation.evaluate_vietsuperspeech_silero_phowhisper import ManifestSample
from ai_training.training.asr.train import VietSuperSpeechDataset, collate_samples


class FakeFeatureExtractor:
    def __init__(self) -> None:
        self.waveform: np.ndarray | None = None
        self.sampling_rate: int | None = None

    def __call__(self, waveform: np.ndarray, sampling_rate: int) -> SimpleNamespace:
        self.waveform = waveform
        self.sampling_rate = sampling_rate
        return SimpleNamespace(input_features=[np.ones((80, 10), dtype=np.float32)])

    def pad(self, features: list[dict[str, np.ndarray]], return_tensors: str) -> dict[str, torch.Tensor]:
        return {"input_features": torch.tensor(np.stack([item["input_features"] for item in features]))}


class FakeTokenizer:
    bos_token_id = 1

    def __call__(self, text: str) -> SimpleNamespace:
        return SimpleNamespace(input_ids=[1, 2, 3])

    def pad(self, features: list[dict[str, list[int]]], return_tensors: str) -> dict[str, torch.Tensor]:
        return {
            "input_ids": torch.tensor([[1, 2, 3], [1, 4, 0]]),
            "attention_mask": torch.tensor([[1, 1, 1], [1, 1, 0]]),
        }


class TrainingInputTest(unittest.TestCase):
    def test_audio_is_resampled_to_mono_16khz(self) -> None:
        with TemporaryDirectory() as directory:
            audio_path = Path(directory) / "sample.wav"
            with io.BytesIO() as buffer:
                with wave.open(buffer, "wb") as writer:
                    writer.setnchannels(2)
                    writer.setsampwidth(2)
                    writer.setframerate(8_000)
                    writer.writeframes(b"\x00\x00\x00\x00" * 8_000)
                audio_path.write_bytes(buffer.getvalue())

            processor = SimpleNamespace(feature_extractor=FakeFeatureExtractor(), tokenizer=FakeTokenizer())
            sample = VietSuperSpeechDataset([ManifestSample(audio_path, "xin chào")], processor, 448)[0]

        self.assertEqual(processor.feature_extractor.sampling_rate, 16_000)
        self.assertEqual(len(processor.feature_extractor.waveform), 16_000)
        self.assertEqual(sample["labels"], [1, 2, 3])
        self.assertEqual(sample["reference"], "xin chào")

    def test_collator_masks_padding_and_removes_bos(self) -> None:
        processor = SimpleNamespace(feature_extractor=FakeFeatureExtractor(), tokenizer=FakeTokenizer())
        samples = [
            {"input_features": np.ones((80, 10), dtype=np.float32), "labels": [1, 2, 3], "reference": "một"},
            {"input_features": np.ones((80, 10), dtype=np.float32), "labels": [1, 4], "reference": "hai"},
        ]

        batch = collate_samples(samples, processor)

        self.assertEqual(batch["input_features"].shape, (2, 80, 10))
        self.assertEqual(batch["labels"].tolist(), [[2, 3], [4, -100]])
        self.assertEqual(batch["references"], ["một", "hai"])


if __name__ == "__main__":
    unittest.main()
