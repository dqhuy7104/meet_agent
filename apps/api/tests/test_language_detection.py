"""Unit tests for Silero Lang 95 routing without downloading weights."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

import numpy as np
import torch

from app.services.audio.language_detection import MAX_LANGUAGE_SAMPLES, SileroLanguageDetector


class FakeLanguageModel:
    def __init__(self, language_index: int) -> None:
        self.language_index = language_index
        self.input_samples = 0

    def get_inputs(self) -> list[SimpleNamespace]:
        return [SimpleNamespace(name="input")]

    def run(self, output_names: object, inputs: dict[str, np.ndarray]) -> list[np.ndarray]:
        self.input_samples = inputs["input"].shape[1]
        logits = np.zeros((1, 95), dtype=np.float32)
        logits[0, self.language_index] = 1
        return [logits]


class LanguageDetectionTest(unittest.TestCase):
    def test_vietnamese_index_routes_to_phowhisper(self) -> None:
        model = FakeLanguageModel(45)
        detector = SileroLanguageDetector(model)  # type: ignore[arg-type]
        self.assertEqual(detector(torch.zeros(16_000)), "vi")

    def test_other_language_uses_only_first_twenty_seconds(self) -> None:
        model = FakeLanguageModel(30)
        detector = SileroLanguageDetector(model)  # type: ignore[arg-type]
        self.assertEqual(detector(torch.zeros(MAX_LANGUAGE_SAMPLES + 16_000)), "other")
        self.assertEqual(model.input_samples, MAX_LANGUAGE_SAMPLES)


if __name__ == "__main__":
    unittest.main()
