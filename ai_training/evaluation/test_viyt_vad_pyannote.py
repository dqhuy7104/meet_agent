"""Evaluate Silero VAD and pyannote speaker diarization on all ViYT-Diar test rows.

The upstream dataset provides a single 100-recording ``test`` split.  Its
speaker turns are used as VAD speech reference and diarization ground truth.

Example:
    export HUGGINGFACE_TOKEN=hf_...
    uv run python ai_training/evaluation/test_viyt_vad_pyannote.py \\
        --dataset-root ai_training/datasets/processed/diarization/ViYT-Diar
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from time import perf_counter
from typing import Any, Iterable


DEFAULT_DATASET_ROOT = Path("ai_training/datasets/processed/diarization/ViYT-Diar")
DEFAULT_OUTPUT = Path("ai_training/results/viyt_vad_pyannote_results.json")


@dataclass(frozen=True)
class Interval:
    """A half-open time interval measured in seconds."""

    start: float
    end: float


@dataclass(frozen=True)
class ReferenceTurn:
    """One labelled speaker turn from the ViYT-Diar dataset."""

    speaker: str
    interval: Interval


@dataclass(frozen=True)
class RecordingResult:
    """Evaluation metrics and timings for one audio recording."""

    audio_id: str
    reference_speakers: int
    predicted_speakers: int
    vad_precision: float
    vad_recall: float
    vad_f1: float
    der: float
    jer: float
    vad_seconds: float
    diarization_seconds: float


def parse_args() -> argparse.Namespace:
    """Parse command-line options for the ViYT-Diar benchmark."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-root", type=Path, default=DEFAULT_DATASET_ROOT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--limit", type=int, help="Evaluate only the first N rows for a smoke test.")
    parser.add_argument(
        "--model",
        default="pyannote/speaker-diarization-community-1",
        help="Hugging Face model ID or local pyannote pipeline path.",
    )
    parser.add_argument(
        "--hf-token-env",
        default="HUGGINGFACE_TOKEN",
        help="Environment-variable name that holds the Hugging Face token.",
    )
    parser.add_argument("--collar", type=float, default=0.0, help="DER/JER collar in seconds.")
    parser.add_argument(
        "--skip-overlap",
        action="store_true",
        help="Exclude overlapping reference speech from DER/JER.",
    )
    return parser.parse_args()


def merge_intervals(intervals: Iterable[Interval]) -> list[Interval]:
    """Return the union of intervals, merging touching intervals."""
    merged: list[Interval] = []
    for current in sorted(intervals, key=lambda interval: (interval.start, interval.end)):
        if not merged or current.start > merged[-1].end:
            merged.append(current)
        else:
            merged[-1] = Interval(merged[-1].start, max(merged[-1].end, current.end))
    return merged


def total_duration(intervals: Iterable[Interval]) -> float:
    """Return the duration of an interval collection after unioning it."""
    return sum(interval.end - interval.start for interval in merge_intervals(intervals))


def intersection_duration(first: Iterable[Interval], second: Iterable[Interval]) -> float:
    """Return duration shared by two interval collections."""
    left = merge_intervals(first)
    right = merge_intervals(second)
    left_index = 0
    right_index = 0
    overlap = 0.0
    while left_index < len(left) and right_index < len(right):
        start = max(left[left_index].start, right[right_index].start)
        end = min(left[left_index].end, right[right_index].end)
        if end > start:
            overlap += end - start
        if left[left_index].end <= right[right_index].end:
            left_index += 1
        else:
            right_index += 1
    return overlap


def vad_scores(reference: Iterable[Interval], hypothesis: Iterable[Interval]) -> tuple[float, float, float]:
    """Compute duration-weighted speech precision, recall, and F1."""
    reference_duration = total_duration(reference)
    hypothesis_duration = total_duration(hypothesis)
    overlap = intersection_duration(reference, hypothesis)
    precision = overlap / hypothesis_duration if hypothesis_duration else 0.0
    recall = overlap / reference_duration if reference_duration else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return precision, recall, f1


def validate_turns(row: dict[str, Any]) -> list[ReferenceTurn]:
    """Convert and validate ViYT-Diar parallel label arrays."""
    speakers = row.get("speaker")
    starts = row.get("timestamps_start")
    ends = row.get("timestamps_end")
    if not all(isinstance(value, list) for value in (speakers, starts, ends)):
        raise ValueError("speaker, timestamps_start, and timestamps_end must be lists")
    if len(speakers) != len(starts) or len(starts) != len(ends):
        raise ValueError("speaker and timestamp arrays must have equal lengths")

    turns: list[ReferenceTurn] = []
    for index, (speaker, start, end) in enumerate(zip(speakers, starts, ends, strict=True)):
        if not isinstance(speaker, str) or not speaker.strip():
            raise ValueError(f"speaker[{index}] must be a non-empty string")
        if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
            raise ValueError(f"timestamps at index {index} must be numeric")
        if start < 0 or end <= start:
            raise ValueError(f"timestamps at index {index} must satisfy 0 <= start < end")
        turns.append(ReferenceTurn(speaker=speaker, interval=Interval(float(start), float(end))))
    return turns


def discover_parquet_files(dataset_root: Path) -> list[Path]:
    """Find downloaded Parquet shards or raise an actionable error."""
    files = sorted(dataset_root.rglob("*.parquet"))
    if not files:
        raise FileNotFoundError(
            f"No Parquet files found under {dataset_root}. Run "
            "ai_training/datasets/processed/diarization/download.py first."
        )
    return files


def load_test_rows(dataset_root: Path, limit: int | None) -> list[dict[str, Any]]:
    """Load all published ViYT-Diar test rows from downloaded Parquet files."""
    if limit is not None and limit < 1:
        raise ValueError("--limit must be at least 1")
    try:
        from datasets import Audio, load_dataset
    except ImportError as error:
        raise RuntimeError("Install project dependencies with `uv sync` before evaluation.") from error

    dataset = load_dataset("parquet", data_files=[str(path) for path in discover_parquet_files(dataset_root)], split="train")
    dataset = dataset.cast_column("audio", Audio(sampling_rate=16_000))
    rows = [dict(row) for row in dataset]
    if not rows:
        raise ValueError("ViYT-Diar Parquet files contained no rows")
    return rows[:limit] if limit is not None else rows


def audio_to_waveform(audio: Any) -> tuple[Any, int]:
    """Convert a decoded Hugging Face audio value to mono torch waveform."""
    try:
        import torch
    except ImportError as error:
        raise RuntimeError("PyTorch is required by Silero VAD and pyannote.") from error

    if hasattr(audio, "get_all_samples"):
        samples = audio.get_all_samples()
        raw_waveform = samples.data
        sample_rate = samples.sample_rate
    elif isinstance(audio, dict) and "array" in audio and "sampling_rate" in audio:
        raw_waveform = audio["array"]
        sample_rate = audio["sampling_rate"]
    else:
        raise ValueError("Audio must decode to a mapping or AudioDecoder with waveform samples")
    if not isinstance(sample_rate, int) or sample_rate <= 0:
        raise ValueError("Audio sampling_rate must be a positive integer")
    waveform = torch.as_tensor(raw_waveform, dtype=torch.float32)
    if waveform.ndim == 2:
        waveform = waveform.mean(dim=1) if waveform.shape[1] <= 8 else waveform.mean(dim=0)
    if waveform.ndim != 1 or waveform.numel() == 0:
        raise ValueError("Audio waveform must be a non-empty mono or stereo array")
    return waveform.contiguous(), sample_rate


def build_reference_annotation(turns: Iterable[ReferenceTurn], uri: str) -> Any:
    """Build a pyannote Annotation preserving overlapping speaker turns."""
    from pyannote.core import Annotation, Segment

    annotation = Annotation(uri=uri)
    for index, turn in enumerate(turns):
        annotation[Segment(turn.interval.start, turn.interval.end), index] = turn.speaker
    return annotation


def extract_diarization_annotation(output: Any) -> Any:
    """Support pyannote 4 DiarizeOutput and legacy Annotation return values."""
    return getattr(output, "speaker_diarization", output)


def predicted_intervals(annotation: Any) -> list[Interval]:
    """Extract speech intervals from a pyannote diarization annotation."""
    return [Interval(float(segment.start), float(segment.end)) for segment, _ in annotation.itertracks()]


def main() -> None:
    """Run VAD and diarization evaluation over the complete ViYT-Diar test set."""
    args = parse_args()
    if args.collar < 0:
        raise ValueError("--collar must be non-negative")
    rows = load_test_rows(args.dataset_root, args.limit)

    try:
        import torch
        from pyannote.audio import Pipeline
        from pyannote.metrics.diarization import DiarizationErrorRate, JaccardErrorRate
        from silero_vad import get_speech_timestamps, load_silero_vad
    except ImportError as error:
        raise RuntimeError("Install project dependencies with `uv sync` before evaluation.") from error

    token = os.environ.get(args.hf_token_env)
    pipeline = Pipeline.from_pretrained(args.model, token=token)
    if pipeline is None:
        raise RuntimeError(f"Could not load pyannote pipeline {args.model!r}")
    if torch.cuda.is_available():
        pipeline.to(torch.device("cuda"))
    vad_model = load_silero_vad()
    global_der = DiarizationErrorRate(collar=args.collar, skip_overlap=args.skip_overlap)
    global_jer = JaccardErrorRate(collar=args.collar, skip_overlap=args.skip_overlap)
    reference_speech_seconds = 0.0
    predicted_speech_seconds = 0.0
    overlap_speech_seconds = 0.0
    results: list[RecordingResult] = []

    for index, row in enumerate(rows, start=1):
        audio_id = str(row.get("audio_id", f"row-{index:03d}"))
        turns = validate_turns(row)
        waveform, sample_rate = audio_to_waveform(row.get("audio"))
        reference = build_reference_annotation(turns, audio_id)
        reference_speech = [turn.interval for turn in turns]

        vad_started = perf_counter()
        vad_timestamps = get_speech_timestamps(waveform, vad_model, sampling_rate=sample_rate)
        vad_elapsed = perf_counter() - vad_started
        vad_intervals = [
            Interval(timestamp["start"] / sample_rate, timestamp["end"] / sample_rate)
            for timestamp in vad_timestamps
        ]

        diarization_started = perf_counter()
        output = pipeline({"waveform": waveform.unsqueeze(0), "sample_rate": sample_rate})
        diarization_elapsed = perf_counter() - diarization_started
        hypothesis = extract_diarization_annotation(output)
        hypothesis_speech = predicted_intervals(hypothesis)
        precision, recall, f1 = vad_scores(reference_speech, vad_intervals)
        der = DiarizationErrorRate(collar=args.collar, skip_overlap=args.skip_overlap)
        jer = JaccardErrorRate(collar=args.collar, skip_overlap=args.skip_overlap)
        result = RecordingResult(
            audio_id=audio_id,
            reference_speakers=len(reference.labels()),
            predicted_speakers=len(hypothesis.labels()),
            vad_precision=precision,
            vad_recall=recall,
            vad_f1=f1,
            der=float(der(reference, hypothesis)),
            jer=float(jer(reference, hypothesis)),
            vad_seconds=vad_elapsed,
            diarization_seconds=diarization_elapsed,
        )
        results.append(result)
        global_der(reference, hypothesis)
        global_jer(reference, hypothesis)
        reference_speech_seconds += total_duration(reference_speech)
        predicted_speech_seconds += total_duration(vad_intervals)
        overlap_speech_seconds += intersection_duration(reference_speech, vad_intervals)
        print(f"[{index}/{len(rows)}] {audio_id}: VAD F1={f1:.3f}, DER={result.der:.3f}, JER={result.jer:.3f}")

    vad_precision = overlap_speech_seconds / predicted_speech_seconds if predicted_speech_seconds else 0.0
    vad_recall = overlap_speech_seconds / reference_speech_seconds if reference_speech_seconds else 0.0
    vad_f1 = 2 * vad_precision * vad_recall / (vad_precision + vad_recall) if vad_precision + vad_recall else 0.0
    report = {
        "dataset": "tuanduy1612/ViYT-Diar",
        "split": "test",
        "recordings": len(results),
        "model": args.model,
        "collar_seconds": args.collar,
        "skip_overlap": args.skip_overlap,
        "vad": {"precision": vad_precision, "recall": vad_recall, "f1": vad_f1},
        "diarization": {"der": float(abs(global_der)), "jer": float(abs(global_jer))},
        "records": [asdict(result) for result in results],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
