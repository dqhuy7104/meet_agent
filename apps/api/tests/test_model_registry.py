"""Unit tests for lazy inference-model initialization."""

from __future__ import annotations

import unittest

from app.core.config import ConfigurationError, Settings
from apps.api.app.core.audio_models import ModelLoadError, ModelLoaders, ModelRegistry


class ModelRegistryTest(unittest.TestCase):
    """Exercise lazy caching without downloading any machine-learning model."""

    def setUp(self) -> None:
        """Create loaders that record calls and return unique sentinels."""
        self.calls: list[str] = []
        self.vad = object()
        self.asr = object()
        self.diarization = object()
        self.loaders = ModelLoaders(
            vad=lambda settings: self._load("vad", self.vad, settings),
            asr=lambda settings: self._load("asr", self.asr, settings),
            diarization=lambda settings: self._load("diarization", self.diarization, settings),
        )
        self.registry = ModelRegistry(Settings(), self.loaders)

    def _load(self, role: str, model: object, settings: Settings) -> object:
        """Record a model load without using the provided settings."""
        del settings
        self.calls.append(role)
        return model

    def test_models_are_not_loaded_at_registry_creation(self) -> None:
        """Registry construction must leave all model loaders untouched."""
        self.assertEqual(self.calls, [])

    def test_each_role_loads_once_and_is_cached(self) -> None:
        """Repeated accesses reuse exactly one instance per model role."""
        self.assertIs(self.registry.get_vad(), self.vad)
        self.assertIs(self.registry.get_vad(), self.vad)
        self.assertIs(self.registry.get_asr(), self.asr)
        self.assertIs(self.registry.get_diarization(), self.diarization)
        self.assertEqual(self.calls, ["vad", "asr", "diarization"])

    def test_a_failed_load_is_wrapped_and_can_retry(self) -> None:
        """A transient failure must not be cached permanently."""
        attempts = 0

        def eventually_loads(settings: Settings) -> object:
            nonlocal attempts
            del settings
            attempts += 1
            if attempts == 1:
                raise RuntimeError("temporary failure")
            return self.vad

        registry = ModelRegistry(Settings(), ModelLoaders(eventually_loads, self.loaders.asr, self.loaders.diarization))
        with self.assertRaises(ModelLoadError):
            registry.get_vad()
        self.assertIs(registry.get_vad(), self.vad)
        self.assertEqual(attempts, 2)


class SettingsTest(unittest.TestCase):
    """Validate environment-based model selection without touching os.environ."""

    def test_environment_selects_model_ids_and_device(self) -> None:
        """Configured values override defaults for the API process."""
        settings = Settings.from_environment(
            {
                "PHOWHISPER_MODEL": "vinai/PhoWhisper-small",
                "PYANNOTE_MODEL": "local-pyannote-pipeline",
                "MODEL_DEVICE": "cpu",
                "HUGGINGFACE_TOKEN": "token",
            }
        )
        self.assertEqual(settings.phowhisper_model, "vinai/PhoWhisper-small")
        self.assertEqual(settings.pyannote_model, "local-pyannote-pipeline")
        self.assertEqual(settings.model_device, "cpu")
        self.assertEqual(settings.huggingface_token, "token")

    def test_invalid_device_is_rejected(self) -> None:
        """Only supported runtime device options are accepted."""
        with self.assertRaises(ConfigurationError):
            Settings.from_environment({"MODEL_DEVICE": "tpu"})


if __name__ == "__main__":
    unittest.main()
