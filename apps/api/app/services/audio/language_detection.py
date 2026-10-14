"""Silero Lang 95 inference on a VAD speech region."""

from __future__ import annotations

import torch
import onnxruntime


MAX_LANGUAGE_SAMPLES = 20 * 16_000
# Index in Silero Lang 95's v3.1 lang_dict_95.json: "vi, Vietnamese".
VIETNAMESE_LANGUAGE_INDEX = 45


class SileroLanguageDetector:
    """Classify speech with the archived 95-language Silero checkpoint."""

    def __init__(
        self, model: onnxruntime.InferenceSession
    ) -> None:
        self.model = model
        self.input_name = model.get_inputs()[0].name
        self.output_name = model.get_outputs()[0].name

    def __call__(self, audio: torch.Tensor) -> str:
        """Return ``vi`` for Vietnamese speech, otherwise ``other``."""
        clip = audio[:MAX_LANGUAGE_SAMPLES].unsqueeze(0).numpy()
        language_logits = self.model.run([self.output_name], {self.input_name: clip})[0]
        language_index = int(language_logits.argmax(axis=1)[0])
        return "vi" if language_index == VIETNAMESE_LANGUAGE_INDEX else "other"
