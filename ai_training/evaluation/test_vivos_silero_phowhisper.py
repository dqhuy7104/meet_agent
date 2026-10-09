"""Smoke-test Silero VAD and PhoWhisper on the VIVOS test split.

Example:
    uv run python ai_training/evaluation/test_vivos_silero_phowhisper.py \
        --dataset-root ai_training/datasets/processed/vivos --limit 5
"""

from __future__ import annotations

import argparse
import json
import re
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path
from time import perf_counter
from typing import Any, Callable


@dataclass(frozen=True)
class SampleResult:
    """Metrics and output for one VIVOS recording."""

    audio_path: str
    reference: str | None
    hypothesis: str
    audio_seconds: float
    speech_seconds: float
    inference_seconds: float
    word_error_rate: float | None
    character_error_rate: float | None


@dataclass(frozen=True)
class PreparedSample:
    """Audio and metadata prepared for one ASR inference request."""

    audio_path: Path
    reference: str | None
    audio_seconds: float
    speech_seconds: float
    speech_audio: Any | None


def parse_args() -> argparse.Namespace:
    """Parse command-line settings for the smoke test."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset-root",
        type=Path,
        default=Path("/home/yuh/.cache/kagglehub/datasets/kynthesis/vivos-vietnamese-speech-corpus-for-asr/versions/1/vivos"),
        help="Directory containing VIVOS train/ and test/ folders.",
    )
    parser.add_argument("--split", choices=("train", "test"), default="test")
    parser.add_argument("--limit", type=int, help="Number of WAV files to evaluate.")
    parser.add_argument(
        "--batch-size",
        type=int,
        default=5,
        help="ASR batch size on CUDA (ignored on CPU, which always uses 1).",
    )
    parser.add_argument(
        "--model",
        default="vinai/PhoWhisper-base",
        help="Hugging Face PhoWhisper model identifier.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("ai_training/results/vivos_phowhisper_results.json"),
        help="Location for the JSON report.",
    )
    return parser.parse_args()


def normalize_text(text: str) -> str:
    """Normalize transcript text before computing error rates."""
    normalized = unicodedata.normalize("NFC", text).lower()
    normalized = re.sub(r"[^\w\s]", " ", normalized, flags=re.UNICODE)
    return " ".join(normalized.split())


def edit_distance(reference: list[str], hypothesis: list[str]) -> int:
    """Return Levenshtein distance for two token sequences."""
    previous = list(range(len(hypothesis) + 1))
    for ref_index, ref_token in enumerate(reference, start=1):
        current = [ref_index]
        for hyp_index, hyp_token in enumerate(hypothesis, start=1):
            substitution = previous[hyp_index - 1] + (ref_token != hyp_token)
            current.append(min(previous[hyp_index] + 1, current[-1] + 1, substitution))
        previous = current
    return previous[-1]


def error_rate(reference: str, hypothesis: str, *, character_level: bool) -> float:
    """Compute WER or CER without an additional metrics dependency."""
    clean_reference = normalize_text(reference)
    clean_hypothesis = normalize_text(hypothesis)
    reference_tokens = list(clean_reference.replace(" ", "")) if character_level else clean_reference.split()
    hypothesis_tokens = list(clean_hypothesis.replace(" ", "")) if character_level else clean_hypothesis.split()
    if not reference_tokens:
        return 0.0 if not hypothesis_tokens else 1.0
    return edit_distance(reference_tokens, hypothesis_tokens) / len(reference_tokens)


def load_prompts(prompts_path: Path) -> dict[str, str]:
    """Read VIVOS ``prompts.txt`` as a mapping of utterance ID to text."""
    if not prompts_path.exists():
        return {}

    prompts: dict[str, str] = {}
    for line in prompts_path.read_text(encoding="utf-8").splitlines():
        utterance_id, separator, transcript = line.strip().partition(" ")
        if separator and transcript.strip():
            prompts[utterance_id] = transcript.strip()
    return prompts


def select_audio_files(waves_dir: Path, limit: int | None) -> list[Path]:
    """Return a deterministic, bounded list of VIVOS WAV files."""
    if limit is not None:
        if limit < 1:
            raise ValueError("--limit must be at least 1")
        audio_files = sorted(waves_dir.rglob("*.wav"))
        if not audio_files:
            raise FileNotFoundError(f"No WAV files found under {waves_dir}")
        return audio_files[:limit]
    return sorted(waves_dir.rglob("*.wav"))


def effective_batch_size(requested_batch_size: int, *, use_cuda: bool) -> int:
    """Return a valid ASR batch size for the selected compute device."""
    if requested_batch_size < 1:
        raise ValueError("--batch-size must be at least 1")
    return requested_batch_size if use_cuda else 1


def load_asr_pipeline(model_id: str) -> tuple[Any, bool]:
    """Load PhoWhisper on CUDA when available, otherwise on CPU."""
    import torch
    from transformers import pipeline

    use_cuda = torch.cuda.is_available()
    pipeline_kwargs: dict[str, Any] = {
        "model": model_id,
        "device": 0 if use_cuda else -1,
    }
    if use_cuda:
        pipeline_kwargs["torch_dtype"] = torch.float16
    return pipeline(
        "automatic-speech-recognition",
        **pipeline_kwargs,
    ), use_cuda


def prepare_speech_regions(audio_paths: list[Path], prompts: dict[str, str]) -> list[PreparedSample]:
    """Apply one shared Silero VAD model to all audio files."""
    import torch
    from silero_vad import get_speech_timestamps, load_silero_vad, read_audio

    sampling_rate = 16_000
    vad_model = load_silero_vad()
    prepared_samples: list[PreparedSample] = []
    for audio_path in audio_paths:
        audio = read_audio(str(audio_path), sampling_rate=sampling_rate)
        speech_timestamps = get_speech_timestamps(audio, vad_model, sampling_rate=sampling_rate)
        if not speech_timestamps:
            prepared_samples.append(
                PreparedSample(
                    audio_path=audio_path,
                    reference=prompts.get(audio_path.stem),
                    audio_seconds=len(audio) / sampling_rate,
                    speech_seconds=0.0,
                    speech_audio=None,
                )
            )
            continue

        speech_audio = torch.cat([audio[item["start"] : item["end"]] for item in speech_timestamps])
        prepared_samples.append(
            PreparedSample(
                audio_path=audio_path,
                reference=prompts.get(audio_path.stem),
                audio_seconds=len(audio) / sampling_rate,
                speech_seconds=len(speech_audio) / sampling_rate,
                speech_audio=speech_audio.numpy(),
            )
        )
    return prepared_samples


def transcribe_prepared_samples(
    prepared_samples: list[PreparedSample],
    asr_pipeline: Any,
    batch_size: int,
    *,
    clock: Callable[[], float] = perf_counter,
) -> tuple[list[str], list[float], float]:
    """Batch transcribe speech while returning per-sample and aggregate ASR time."""
    speech_sample_indices = [index for index, sample in enumerate(prepared_samples) if sample.speech_audio is not None]
    if not speech_sample_indices:
        return ["" for _ in prepared_samples], [0.0 for _ in prepared_samples], 0.0

    transcripts = ["" for _ in prepared_samples]
    inference_seconds = [0.0 for _ in prepared_samples]
    total_inference_seconds = 0.0
    for batch_start in range(0, len(speech_sample_indices), batch_size):
        batch_indices = speech_sample_indices[batch_start : batch_start + batch_size]
        asr_inputs = [
            {"array": prepared_samples[index].speech_audio, "sampling_rate": 16_000}
            for index in batch_indices
        ]
        started_at = clock()
        raw_results = asr_pipeline(asr_inputs, batch_size=len(batch_indices))
        batch_seconds = clock() - started_at
        results = [raw_results] if isinstance(raw_results, dict) else raw_results
        if len(results) != len(batch_indices):
            raise RuntimeError("ASR pipeline returned a different number of results than inputs")

        seconds_per_sample = batch_seconds / len(batch_indices)
        total_inference_seconds += batch_seconds
        for sample_index, result in zip(batch_indices, results, strict=True):
            transcripts[sample_index] = str(result["text"]).strip()
            inference_seconds[sample_index] = seconds_per_sample
    return transcripts, inference_seconds, total_inference_seconds


def main() -> None:
    """Run VAD followed by ASR, print aggregate metrics, and save a report."""
    args = parse_args()
    split_dir = args.dataset_root / args.split
    audio_files = select_audio_files(split_dir / "waves", args.limit)
    prompts = load_prompts(split_dir / "prompts.txt")
    asr_pipeline, use_cuda = load_asr_pipeline(args.model)
    batch_size = effective_batch_size(args.batch_size, use_cuda=use_cuda)
    print(f"ASR device: {'cuda:0' if use_cuda else 'cpu'}; effective batch size: {batch_size}")
    prepared_samples = prepare_speech_regions(audio_files, prompts)
    hypotheses, inference_seconds, total_inference_seconds = transcribe_prepared_samples(
        prepared_samples, asr_pipeline, batch_size
    )

    results: list[SampleResult] = []
    for sample, hypothesis, sample_inference_seconds in zip(
        prepared_samples, hypotheses, inference_seconds, strict=True
    ):
        reference = sample.reference
        results.append(
            SampleResult(
                audio_path=str(sample.audio_path),
                reference=reference,
                hypothesis=hypothesis,
                audio_seconds=sample.audio_seconds,
                speech_seconds=sample.speech_seconds,
                inference_seconds=sample_inference_seconds,
                word_error_rate=(error_rate(reference, hypothesis, character_level=False) if reference else None),
                character_error_rate=(error_rate(reference, hypothesis, character_level=True) if reference else None),
            )
        )

    scored_results = [result for result in results if result.reference is not None]
    report = {
        "dataset_root": str(args.dataset_root),
        "split": args.split,
        "model": args.model,
        "samples": [asdict(result) for result in results],
        "total_inference_seconds": total_inference_seconds,
        "mean_inference_seconds": (
            total_inference_seconds / len(scored_results) if scored_results else None
        ),
        "mean_wer": (
            sum(result.word_error_rate or 0.0 for result in scored_results) / len(scored_results)
            if scored_results
            else None
        ),
        "mean_cer": (
            sum(result.character_error_rate or 0.0 for result in scored_results) / len(scored_results)
            if scored_results
            else None
        ),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("Report:", json.dumps(report, ensure_ascii=False, indent=2))
 

if __name__ == "__main__":
    main()
