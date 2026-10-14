"""Environment-backed configuration for the API and worker processes."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Mapping


class ConfigurationError(ValueError):
    """Raised when API configuration is invalid."""


@dataclass(frozen=True)
class Settings:
    """Validated model and local meeting storage settings."""

    phowhisper_model: str = "vinai/PhoWhisper-small"
    whisper_model: str = "openai/whisper-small"
    silero_lang95_model: str = (
        "https://huggingface.co/Derur/silero-models/resolve/"
        "ab3dfc803ab6384ac507dc81f0d46fd1613058b8/lang95/lang_classifier_95.onnx"
    )
    pyannote_model: str = "pyannote/speaker-diarization-community-1"
    model_device: str = "auto"
    huggingface_token: str | None = None
    meeting_data_dir: Path = Path("./meeting-data")

    @classmethod
    def from_environment(cls, environment: Mapping[str, str] | None = None) -> "Settings":
        """Create settings from environment variables without mutating them."""
        values = os.environ if environment is None else environment
        phowhisper_model = values.get("PHOWHISPER_MODEL", cls.phowhisper_model).strip()
        whisper_model = values.get("WHISPER_MODEL", cls.whisper_model).strip()
        silero_lang95_model = values.get("SILERO_LANG95_MODEL", cls.silero_lang95_model).strip()
        pyannote_model = values.get("PYANNOTE_MODEL", cls.pyannote_model).strip()
        model_device = values.get("MODEL_DEVICE", cls.model_device).strip().lower()
        huggingface_token = values.get("HUGGINGFACE_TOKEN", "").strip() or None
        meeting_data_dir = values.get("MEETING_DATA_DIR", str(cls.meeting_data_dir)).strip()
        if not phowhisper_model:
            raise ConfigurationError("PHOWHISPER_MODEL must not be empty")
        if not whisper_model:
            raise ConfigurationError("WHISPER_MODEL must not be empty")
        if not silero_lang95_model:
            raise ConfigurationError("SILERO_LANG95_MODEL must not be empty")
        if not pyannote_model:
            raise ConfigurationError("PYANNOTE_MODEL must not be empty")
        if model_device not in {"auto", "cpu", "cuda"}:
            raise ConfigurationError("MODEL_DEVICE must be one of: auto, cpu, cuda")
        if not meeting_data_dir:
            raise ConfigurationError("MEETING_DATA_DIR must not be empty")
        return cls(
            phowhisper_model=phowhisper_model,
            whisper_model=whisper_model,
            silero_lang95_model=silero_lang95_model,
            pyannote_model=pyannote_model,
            model_device=model_device,
            huggingface_token=huggingface_token,
            meeting_data_dir=Path(meeting_data_dir).expanduser().resolve(),
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return process-wide immutable API settings."""
    return Settings.from_environment()
