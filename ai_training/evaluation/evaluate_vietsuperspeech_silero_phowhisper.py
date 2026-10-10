"""Evaluate Silero VAD and PhoWhisper on a VietSuperSpeech split.

Example:
    uv run python ai_training/evaluation/evaluate_vietsuperspeech_silero_phowhisper.py \
        --dataset-root ai_training/datasets/processed/Vietsuperspeech/VietSuperSpeech \
        --limit 5
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

try:
    from ai_training.evaluation.test_vivos_silero_phowhisper import (
        PreparedSample,
        SampleResult,
        effective_batch_size,
        error_rate,
        load_asr_pipeline,
        transcribe_prepared_samples,
    )
except ModuleNotFoundError:
    # Permit the documented ``python ai_training/evaluation/...py`` invocation.
    from test_vivos_silero_phowhisper import (  # type: ignore[no-redef]
        PreparedSample,
        SampleResult,
        effective_batch_size,
        error_rate,
        load_asr_pipeline,
        transcribe_prepared_samples,
    )


DEFAULT_DATASET_ROOT = Path("ai_training/datasets/processed/Vietsuperspeech/VietSuperSpeech")
DEFAULT_OUTPUT = Path("ai_training/results/vietsuperspeech_phowhisper_results.json")


@dataclass(frozen=True)
class ManifestSample:
    """One VietSuperSpeech manifest entry with a resolved WAV path."""

    audio_path: Path
    reference: str


def parse_args() -> argparse.Namespace:
    """Parse command-line settings for the VietSuperSpeech evaluation."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-root", type=Path, default=DEFAULT_DATASET_ROOT)
    parser.add_argument("--split", choices=("dev", "train"), default="dev")
    parser.add_argument("--limit", type=int, help="Number of manifest entries to evaluate.")
    parser.add_argument(
        "--batch-size",
        type=int,
        default=5,
        help="ASR batch size on CUDA (CPU always uses one item).",
    )
    parser.add_argument("--model", default="vinai/PhoWhisper-base", help="Hugging Face model identifier.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="Location for the JSON report.")
    return parser.parse_args()


def resolve_audio_path(dataset_root: Path, relative_path: str) -> Path:
    """Resolve a manifest audio path and reject paths outside the dataset root."""
    if not relative_path.strip():
        raise ValueError("Manifest audio path must be non-empty")
    root = dataset_root.resolve()
    candidate = (root / relative_path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise ValueError(f"Manifest audio path escapes dataset root: {relative_path!r}") from error
    if not candidate.is_file():
        raise FileNotFoundError(f"Manifest audio file does not exist: {candidate}")
    return candidate


def load_manifest_samples(dataset_root: Path, split: str, limit: int | None) -> list[ManifestSample]:
    """Load and validate one VietSuperSpeech JSON manifest in its stored order."""
    if limit is not None and limit < 1:
        raise ValueError("--limit must be at least 1")
    manifest_path = dataset_root / f"{split}.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"VietSuperSpeech manifest does not exist: {manifest_path}")
    try:
        entries = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"Invalid JSON in manifest {manifest_path}: {error.msg}") from error
    if not isinstance(entries, list):
        raise ValueError(f"Manifest {manifest_path} must contain a JSON array")

    samples: list[ManifestSample] = []
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            raise ValueError(f"Manifest entry {index} must be an object")
        audio = entry.get("audio")
        text = entry.get("text")
        if not isinstance(audio, str):
            raise ValueError(f"Manifest entry {index} field 'audio' must be a string")
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f"Manifest entry {index} field 'text' must be a non-empty string")
        samples.append(ManifestSample(resolve_audio_path(dataset_root, audio), text.strip()))
        if limit is not None and len(samples) == limit:
            break
    if not samples:
        raise ValueError(f"Manifest {manifest_path} contains no samples")
    return samples


def prepare_speech_regions(samples: list[ManifestSample]) -> list[PreparedSample]:
    """Apply Silero VAD to the manifest samples while preserving their references."""
    import torch
    from silero_vad import get_speech_timestamps, load_silero_vad, read_audio

    sampling_rate = 16_000
    vad_model = load_silero_vad()
    prepared: list[PreparedSample] = []
    for sample in samples:
        audio = read_audio(str(sample.audio_path), sampling_rate=sampling_rate)
        timestamps = get_speech_timestamps(audio, vad_model, sampling_rate=sampling_rate)
        speech_audio = (
            torch.cat([audio[item["start"] : item["end"]] for item in timestamps]) if timestamps else None
        )
        prepared.append(
            PreparedSample(
                audio_path=sample.audio_path,
                reference=sample.reference,
                audio_seconds=len(audio) / sampling_rate,
                speech_seconds=(len(speech_audio) / sampling_rate if speech_audio is not None else 0.0),
                speech_audio=(speech_audio.numpy() if speech_audio is not None else None),
            )
        )
    return prepared


def main() -> None:
    """Run VAD followed by ASR and write a VietSuperSpeech evaluation report."""
    args = parse_args()
    samples = load_manifest_samples(args.dataset_root, args.split, args.limit)
    asr_pipeline, use_cuda = load_asr_pipeline(args.model)
    batch_size = effective_batch_size(args.batch_size, use_cuda=use_cuda)
    print(f"ASR device: {'cuda:0' if use_cuda else 'cpu'}; effective batch size: {batch_size}")
    prepared = prepare_speech_regions(samples)
    hypotheses, inference_seconds, total_inference_seconds = transcribe_prepared_samples(
        prepared, asr_pipeline, batch_size
    )
    results = [
        SampleResult(
            audio_path=str(sample.audio_path),
            reference=sample.reference,
            hypothesis=hypothesis,
            audio_seconds=sample.audio_seconds,
            speech_seconds=sample.speech_seconds,
            inference_seconds=sample_seconds,
            word_error_rate=error_rate(sample.reference, hypothesis, character_level=False),
            character_error_rate=error_rate(sample.reference, hypothesis, character_level=True),
        )
        for sample, hypothesis, sample_seconds in zip(prepared, hypotheses, inference_seconds, strict=True)
    ]
    report = {
        "dataset_root": str(args.dataset_root),
        "split": args.split,
        "model": args.model,
        "samples": [asdict(result) for result in results],
        "total_inference_seconds": total_inference_seconds,
        "mean_inference_seconds": total_inference_seconds / len(results),
        "mean_wer": sum(result.word_error_rate for result in results if result.word_error_rate is not None) / len(results),
        "mean_cer": sum(result.character_error_rate for result in results if result.character_error_rate is not None) / len(results),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("Report:", json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
