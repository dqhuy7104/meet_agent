"""Compare local voice samples from an identical diarization pipeline."""

from dataclasses import dataclass
from hashlib import sha256
from importlib.metadata import version
from pathlib import Path
import re
from typing import Mapping, Sequence

import numpy as np
import yaml

from app.core.config import ConfigurationError, Settings


@dataclass(frozen=True)
class VoiceMatch:
    profile_id: str
    similarity: float


def normalize_embedding(values: Sequence[float]) -> list[float] | None:
    """Reject unusable centroids and return a unit vector."""
    vector = np.asarray(values, dtype=float)
    if vector.ndim != 1 or not vector.size or not np.isfinite(vector).all():
        return None
    norm = float(np.linalg.norm(vector))
    if not np.isfinite(norm) or norm == 0:
        return None
    return (vector / norm).tolist()


def match_speakers(
    speakers: Mapping[str, Sequence[float]], profiles: Mapping[str, Sequence[Sequence[float]]],
    min_cosine: float, min_margin: float,
) -> dict[str, VoiceMatch | None]:
    """Match normalized speaker centroids against normalized profile means."""
    matches: dict[str, VoiceMatch | None] = {}
    for speaker, values in speakers.items():
        matches[speaker] = None
        vector = normalize_embedding(values)
        if vector is None:
            continue
        scores = []
        for profile_id, samples in profiles.items():
            eligible = [normalized for sample in samples
                        if (normalized := normalize_embedding(sample)) is not None and len(normalized) == len(vector)]
            if not eligible:
                continue
            centroid = normalize_embedding(np.mean(eligible, axis=0))
            if centroid is not None:
                scores.append(VoiceMatch(profile_id, float(np.clip(np.dot(vector, centroid), -1, 1))))
        scores.sort(key=lambda match: (-match.similarity, match.profile_id))
        if scores and scores[0].similarity >= min_cosine:
            if len(scores) == 1 or scores[0].similarity - scores[1].similarity >= min_margin:
                matches[speaker] = scores[0]
    return matches


def pipeline_fingerprint(settings: Settings) -> str:
    """Identify the pinned pipeline, embedding weights, and preprocessing."""
    path = Path(settings.pyannote_model)
    digest = sha256()
    if path.exists():
        config_path = path / 'config.yaml' if path.is_dir() else path
        config_bytes = config_path.read_bytes()
        config = yaml.safe_load(config_bytes)
        embedding = Path(config['pipeline']['params']['embedding'])
        if not embedding.is_absolute():
            embedding = config_path.parent / embedding
        digest.update(config_bytes)
        with embedding.open('rb') as checkpoint:
            for chunk in iter(lambda: checkpoint.read(1024 * 1024), b''):
                digest.update(chunk)
    else:
        if not settings.pyannote_revision or not re.fullmatch(r'[0-9a-fA-F]{40}', settings.pyannote_revision):
            raise ConfigurationError('PYANNOTE_REVISION must be an immutable 40-character commit for voice identification')
        digest.update(f'{settings.pyannote_model}@{settings.pyannote_revision.lower()}'.encode())
    digest.update(f'|pyannote.audio={version("pyannote.audio")}|16000 Hz mono float32'.encode())
    return digest.hexdigest()
