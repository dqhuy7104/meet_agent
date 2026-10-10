"""Lazy, process-local registry for inference models."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from threading import Lock
from time import perf_counter
from typing import Any, Callable

from app.core.config import Settings, get_settings


logger = logging.getLogger(__name__)
ModelLoader = Callable[[Settings], Any]


class ModelLoadError(RuntimeError):
    """Raised when an inference model cannot be loaded on first use."""

    def __init__(self, role: str, identifier: str, reason: Exception) -> None:
        """Describe the failed model role, identifier, and original reason."""
        super().__init__(f"Could not load {role} model {identifier!r}: {reason}")
        self.role = role
        self.identifier = identifier


@dataclass(frozen=True)
class ModelLoaders:
    """Injectable model loader functions, primarily for isolated tests."""

    vad: ModelLoader
    asr: ModelLoader
    diarization: ModelLoader


def _use_cuda(settings: Settings) -> bool:
    """Resolve the configured device only when a model is actually loading."""
    import torch

    if settings.model_device == "cpu":
        return False
    if settings.model_device == "cuda" and not torch.cuda.is_available():
        raise RuntimeError("MODEL_DEVICE=cuda was requested but CUDA is not available")
    return torch.cuda.is_available()


def load_vad(settings: Settings) -> Any:
    """Load the shared Silero VAD model on demand."""
    del settings
    from silero_vad import load_silero_vad

    return load_silero_vad()


def load_phowhisper(settings: Settings) -> Any:
    """Load the configured PhoWhisper ASR pipeline on demand."""
    import torch
    from transformers import pipeline

    use_cuda = _use_cuda(settings)
    options: dict[str, Any] = {
        "model": settings.phowhisper_model,
        "device": 0 if use_cuda else -1,
    }
    if use_cuda:
        options["torch_dtype"] = torch.float16
    return pipeline("automatic-speech-recognition", **options)


def load_pyannote(settings: Settings) -> Any:
    """Load the configured pyannote speaker-diarization pipeline on demand."""
    import torch
    from pyannote.audio import Pipeline

    is_local_pipeline = Path(settings.pyannote_model).exists()
    if not is_local_pipeline and not settings.huggingface_token:
        raise RuntimeError(
            "HUGGINGFACE_TOKEN is required to load a remote pyannote pipeline; "
            "accept the model agreement on Hugging Face first"
        )
    pipeline = Pipeline.from_pretrained(settings.pyannote_model, token=settings.huggingface_token)
    if pipeline is None:
        raise RuntimeError(f"pyannote returned no pipeline for {settings.pyannote_model!r}")
    if _use_cuda(settings):
        pipeline.to(torch.device("cuda"))
    return pipeline


DEFAULT_LOADERS = ModelLoaders(vad=load_vad, asr=load_phowhisper, diarization=load_pyannote)


class ModelRegistry:
    """Create each inference model once, at its first use, per API process."""

    def __init__(self, settings: Settings, loaders: ModelLoaders = DEFAULT_LOADERS) -> None:
        """Create an empty registry without loading any inference model."""
        self._settings = settings
        self._loaders = loaders
        self._vad: Any | None = None
        self._asr: Any | None = None
        self._diarization: Any | None = None
        self._vad_lock = Lock()
        self._asr_lock = Lock()
        self._diarization_lock = Lock()

    def get_vad(self) -> Any:
        """Return the cached Silero VAD model, loading it on first request."""
        return self._get("vad", "silero-vad", "_vad", self._vad_lock, self._loaders.vad)

    def get_asr(self) -> Any:
        """Return the cached configured PhoWhisper pipeline on first request."""
        return self._get("asr", self._settings.phowhisper_model, "_asr", self._asr_lock, self._loaders.asr)

    def get_diarization(self) -> Any:
        """Return the cached configured pyannote pipeline on first request."""
        return self._get(
            "diarization",
            self._settings.pyannote_model,
            "_diarization",
            self._diarization_lock,
            self._loaders.diarization,
        )

    def _get(
        self,
        role: str,
        identifier: str,
        cache_attribute: str,
        lock: Lock,
        loader: ModelLoader,
    ) -> Any:
        """Load a role once while allowing retry after a failed initialization."""
        cached = getattr(self, cache_attribute)
        if cached is not None:
            return cached
        with lock:
            cached = getattr(self, cache_attribute)
            if cached is not None:
                return cached
            started_at = perf_counter()
            logger.info("Loading %s model %s", role, identifier)
            try:
                loaded = loader(self._settings)
            except Exception as error:
                logger.exception("Failed to load %s model %s", role, identifier)
                raise ModelLoadError(role, identifier, error) from error
            setattr(self, cache_attribute, loaded)
            logger.info("Loaded %s model %s in %.2fs", role, identifier, perf_counter() - started_at)
            return loaded


@lru_cache(maxsize=1)
def get_model_registry() -> ModelRegistry:
    """Return the shared empty-or-lazy-loaded registry for this API process."""
    return ModelRegistry(get_settings())
