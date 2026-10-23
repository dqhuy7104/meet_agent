"""Lazy, process-local registry for inference models."""

from __future__ import annotations

import logging
import os
import tempfile
from hashlib import sha256
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from threading import Lock
from time import perf_counter
from typing import Any, Callable
from urllib.parse import urlparse

from app.core.config import Settings, get_settings
from app.services.audio.language_detection import SileroLanguageDetector


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
    language: ModelLoader
    asr: ModelLoader
    whisper: ModelLoader
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


def load_whisper(settings: Settings) -> Any:
    """Load the configured multilingual OpenAI Whisper checkpoint locally."""
    import torch
    from transformers import pipeline

    use_cuda = _use_cuda(settings)
    options: dict[str, Any] = {
        "model": settings.whisper_model,
        "device": 0 if use_cuda else -1,
    }
    if use_cuda:
        options["torch_dtype"] = torch.float16
    return pipeline("automatic-speech-recognition", **options)


def load_silero_lang95(settings: Settings) -> SileroLanguageDetector:
    """Load Silero Lang 95 ONNX weights from a local path or cached download."""
    import onnxruntime
    import torch

    source = settings.silero_lang95_model
    if urlparse(source).scheme in {"http", "https"}:
        cache_dir = Path(torch.hub.get_dir()) / "silero-lang95"
        cache_dir.mkdir(parents=True, exist_ok=True)
        source_hash = sha256(source.encode()).hexdigest()[:16]
        model_path = cache_dir / f"lang_classifier_95-{source_hash}.onnx"
        if not model_path.exists():
            descriptor, temporary_path = tempfile.mkstemp(dir=cache_dir, suffix=".onnx")
            os.close(descriptor)
            try:
                torch.hub.download_url_to_file(source, temporary_path)
                os.replace(temporary_path, model_path)
            finally:
                if os.path.exists(temporary_path):
                    os.unlink(temporary_path)
    else:
        model_path = Path(source)
    if source == Settings.silero_lang95_model:
        actual_hash = sha256(model_path.read_bytes()).hexdigest()
        expected_hash = "12a91f706ad6ca5a5eba4e84a96739d077e56f121ec71db86a752f62e218a227"
        if actual_hash != expected_hash:
            raise RuntimeError("Silero Lang 95 checkpoint checksum mismatch")
    use_cuda = _use_cuda(settings)
    available_providers = onnxruntime.get_available_providers()
    if settings.model_device == "cuda" and "CUDAExecutionProvider" not in available_providers:
        raise RuntimeError("MODEL_DEVICE=cuda requires an ONNX Runtime CUDA provider for Silero Lang 95")
    provider = "CUDAExecutionProvider" if use_cuda and "CUDAExecutionProvider" in available_providers else "CPUExecutionProvider"
    model = onnxruntime.InferenceSession(str(model_path), providers=[provider])
    return SileroLanguageDetector(model)


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
    options = {"token": settings.huggingface_token}
    if not is_local_pipeline and settings.pyannote_revision is not None:
        options["revision"] = settings.pyannote_revision
    pipeline = Pipeline.from_pretrained(settings.pyannote_model, **options)
    if pipeline is None:
        raise RuntimeError(f"pyannote returned no pipeline for {settings.pyannote_model!r}")
    if _use_cuda(settings):
        pipeline.to(torch.device("cuda"))
    return pipeline


DEFAULT_LOADERS = ModelLoaders(
    vad=load_vad,
    language=load_silero_lang95,
    asr=load_phowhisper,
    whisper=load_whisper,
    diarization=load_pyannote,
)


class ModelRegistry:
    """Create each inference model once, at its first use, per API process."""

    def __init__(self, settings: Settings, loaders: ModelLoaders = DEFAULT_LOADERS) -> None:
        """Create an empty registry without loading any inference model."""
        self._settings = settings
        self._loaders = loaders
        self._vad: Any | None = None
        self._language: Any | None = None
        self._asr: Any | None = None
        self._whisper: Any | None = None
        self._diarization: Any | None = None
        self._vad_lock = Lock()
        self._language_lock = Lock()
        self._asr_lock = Lock()
        self._whisper_lock = Lock()
        self._diarization_lock = Lock()

    def get_vad(self) -> Any:
        """Return the cached Silero VAD model, loading it on first request."""
        return self._get("vad", "silero-vad", "_vad", self._vad_lock, self._loaders.vad)

    def get_asr(self) -> Any:
        """Return the cached configured PhoWhisper pipeline on first request."""
        return self._get("asr", self._settings.phowhisper_model, "_asr", self._asr_lock, self._loaders.asr)

    def get_language(self) -> Any:
        """Return the cached Silero Lang 95 classifier on first request."""
        return self._get(
            "language", self._settings.silero_lang95_model, "_language", self._language_lock, self._loaders.language
        )

    def get_whisper(self) -> Any:
        """Return the cached multilingual Whisper pipeline on first request."""
        return self._get(
            "whisper", self._settings.whisper_model, "_whisper", self._whisper_lock, self._loaders.whisper
        )

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
