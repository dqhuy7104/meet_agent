"""Offline, local-only voice calibration using production extraction and matching.

FAR counts accepted impostor trials (unknown probes or a wrong top identity).
FRR counts rejected known probes; correct identification counts correct accepted
known probes. Rates are empirical observations, not confidence guarantees.
"""
from __future__ import annotations

import argparse
import csv
from dataclasses import asdict, dataclass
from importlib.metadata import version
import json
import math
from pathlib import Path
import sys
from time import perf_counter
from typing import TYPE_CHECKING, Any, Sequence, TextIO

# Also support direct invocation from the repository root.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'apps/api'))
if TYPE_CHECKING:
    from app.core.audio_models import ModelRegistry

MAX_AUDIO_BYTES = 25 * 1024 * 1024


@dataclass(frozen=True)
class ScoredProbe:
    top_similarity: float
    runner_up_similarity: float | None
    is_known: bool
    top_is_correct: bool


@dataclass(frozen=True)
class CalibrationResult:
    min_cosine: float
    min_margin: float
    false_accept_rate: float
    false_reject_rate: float
    unknown_rate: float
    correct_identification_rate: float


@dataclass(frozen=True)
class ManifestRow:
    audio_path: Path
    person_id: str
    session_id: str
    role: str


def read_manifest(source: TextIO, base_dir: Path = Path('.')) -> list[ManifestRow]:
    reader = csv.DictReader(source)
    if not {'audio_path', 'person_id', 'session_id', 'role'} <= set(reader.fieldnames or []):
        raise ValueError('Manifest requires audio_path, person_id, session_id, role')
    rows = []
    for number, values in enumerate(reader, 2):
        fields = [(values.get(key) or '').strip() for key in ('audio_path', 'person_id', 'session_id', 'role')]
        if not all(fields) or fields[3] not in {'enrollment', 'probe'}:
            raise ValueError(f'Invalid manifest row {number}')
        rows.append(ManifestRow(base_dir / fields[0], *fields[1:]))
    sessions: dict[str, set[str]] = {}
    for row in rows:
        if row.role == 'enrollment':
            sessions.setdefault(row.person_id, set()).add(row.session_id)
    probes = [row for row in rows if row.role == 'probe']
    if any(row.session_id in sessions.get(row.person_id, set()) for row in probes):
        raise ValueError('Enrollment and probe sessions must differ for each person')
    if not sessions or not any(row.person_id in sessions for row in probes) or not any(
        row.person_id not in sessions for row in probes
    ):
        raise ValueError('Manifest requires enrollment, known probes and unknown probes')
    return rows


def choose_threshold(
    scores: Sequence[ScoredProbe], thresholds: Sequence[float], margins: Sequence[float],
) -> CalibrationResult | None:
    if any(not math.isfinite(value) or not -1 <= value <= 1 for value in thresholds):
        raise ValueError('Cosine thresholds must be finite and within [-1, 1]')
    if any(not math.isfinite(value) or not 0 <= value <= 2 for value in margins):
        raise ValueError('Margins must be finite and within [0, 2]')
    known_count = sum(score.is_known for score in scores)
    negative_count = sum(not score.is_known or not score.top_is_correct for score in scores)
    if not known_count or not negative_count:
        return None
    best = None
    for threshold in thresholds:
        for margin in margins:
            accepted = [math.isfinite(score.top_similarity) and score.top_similarity >= threshold and (
                score.runner_up_similarity is None or score.top_similarity - score.runner_up_similarity >= margin
            ) for score in scores]
            false_accepts = sum(accept and (not score.is_known or not score.top_is_correct)
                                for score, accept in zip(scores, accepted))
            result = CalibrationResult(
                threshold, margin, false_accepts / negative_count,
                sum(score.is_known and not accept for score, accept in zip(scores, accepted)) / known_count,
                1 - sum(accepted) / len(scores),
                sum(score.is_known and score.top_is_correct and accept
                    for score, accept in zip(scores, accepted)) / known_count,
            )
            if result.false_accept_rate <= .01 and (
                best is None or result.correct_identification_rate > best.correct_identification_rate
            ):
                best = result
    return best


def evaluate_manifest(
    rows: Sequence[ManifestRow], models: ModelRegistry, fingerprint: str,
    thresholds: Sequence[float], margins: Sequence[float],
) -> dict[str, Any]:
    from app.services.voice.enrollment import extract_upload_embedding
    from app.services.voice.matching import match_speakers

    profiles: dict[str, list[list[float]]] = {}
    probes = []
    latencies = []
    for row in rows:
        started = perf_counter()
        with row.audio_path.open('rb') as source:
            audio = source.read(MAX_AUDIO_BYTES + 1)
        if len(audio) > MAX_AUDIO_BYTES:
            raise ValueError('Audio exceeds 25 MiB')
        vector = extract_upload_embedding(audio, models)
        latencies.append(perf_counter() - started)
        if row.role == 'enrollment':
            samples = profiles.setdefault(row.person_id, [])
            samples.append(vector)
            del samples[:-5]
        else:
            probes.append((row.person_id, vector))
    scores = []
    for person, vector in probes:
        ranked = []
        for profile, samples in profiles.items():
            match = match_speakers({'probe': vector}, {profile: samples}, -1, 0)['probe']
            if match is not None:
                ranked.append(match)
        ranked.sort(key=lambda match: (-match.similarity, match.profile_id))
        scores.append(ScoredProbe(
            ranked[0].similarity if ranked else -math.inf,
            ranked[1].similarity if len(ranked) > 1 else None,
            person in profiles, bool(ranked and ranked[0].profile_id == person),
        ))
    result = choose_threshold(scores, thresholds, margins)
    return {
        'sample_count': len(rows), 'enrollment_count': sum(row.role == 'enrollment' for row in rows),
        'retained_enrollment_count': sum(map(len, profiles.values())), 'profile_count': len(profiles),
        'probe_count': len(probes), 'known_probe_count': sum(score.is_known for score in scores),
        'unknown_probe_count': sum(not score.is_known for score in scores),
        'negative_trial_count': sum(not score.is_known or not score.top_is_correct for score in scores),
        'fingerprint': fingerprint, 'preprocessing': 'FFmpeg 16000 Hz mono float32; one speaker; >=10s exclusive speech; <=120s',
        'extraction_latency_seconds': {'total': sum(latencies), 'mean': sum(latencies) / len(latencies)},
        'latency_scope': 'file read, decode and extraction; includes lazy model loading on first sample',
        'calibration': asdict(result) if result else None,
        'threshold_candidates': list(thresholds), 'margin_candidates': list(margins),
        'far_definition': 'accepted unknown or wrong-top known probes / unknown or wrong-top known probes',
        'frr_definition': 'rejected known probes / known probes',
        'unknown_rate_definition': 'rejected probes / all probes',
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--dataset-version', required=True)
    parser.add_argument('--thresholds', type=float, nargs='+', required=True)
    parser.add_argument('--margins', type=float, nargs='+', required=True)
    args = parser.parse_args()
    from app.core.config import Settings
    from app.core.audio_models import ModelRegistry
    from app.services.voice.matching import pipeline_fingerprint

    try:
        with args.manifest.open(newline='') as source:
            rows = read_manifest(source, args.manifest.parent)
        settings = Settings.from_environment()
        fingerprint = pipeline_fingerprint(settings)
        report = evaluate_manifest(rows, ModelRegistry(settings), fingerprint, args.thresholds, args.margins)
    except Exception:
        # File/model exceptions can contain private filenames, tokens or contents.
        parser.exit(1, 'Calibration failed: check manifest, local audio and pinned model configuration privately.\n')
    model_id = 'local-pipeline' if Path(settings.pyannote_model).exists() else settings.pyannote_model
    report.update(dataset_version=args.dataset_version, model_id=model_id,
                  model_revision=settings.pyannote_revision, device=settings.model_device,
                  dependencies={name: version(name) for name in ('pyannote.audio', 'torch', 'numpy')})
    args.output.write_text(json.dumps(report, indent=2) + '\n')


if __name__ == '__main__':
    main()
