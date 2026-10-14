"""Evaluate API-style VAD, language routing, and ASR on VietSuperSpeech.

Example:
    PYTHONPATH=apps/api uv run python ai_training/evaluation/evaluate_vietsuperspeech_silero_phowhisper.py \
        --dataset-root ai_training/datasets/processed/Vietsuperspeech/VietSuperSpeech \
        --limit 5
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass, replace
from hashlib import sha256
from importlib.metadata import version
from pathlib import Path
from time import perf_counter
from typing import Any, Callable, Protocol

import numpy as np
import torch

try:
    from ai_training.evaluation.test_vivos_silero_phowhisper import error_rate
except ModuleNotFoundError:
    # Permit the documented ``python ai_training/evaluation/...py`` invocation.
    from test_vivos_silero_phowhisper import error_rate  # type: ignore[no-redef]


DEFAULT_DATASET_ROOT = Path("ai_training/datasets/processed/Vietsuperspeech/VietSuperSpeech")
DEFAULT_OUTPUT = Path("ai_training/results/vietsuperspeech_routed_asr_results.json")


@dataclass(frozen=True)
class ManifestSample:
    """One VietSuperSpeech manifest entry with a resolved WAV path."""

    audio_path: Path
    reference: str


@dataclass(frozen=True)
class PreparedSample:
    """Decoded recording with its original VAD regions kept separate."""

    audio_path: Path
    reference: str
    audio_seconds: float
    speech_seconds: float
    sampling_rate: int
    speech_regions: list[np.ndarray]


@dataclass(frozen=True)
class RoutedTranscription:
    """One recording's transcript and per-region routing decisions."""

    hypothesis: str
    languages: list[str]
    asr_routes: list[str]
    inference_seconds: float


@dataclass(frozen=True)
class RoutedSampleResult:
    """Metrics and routing details for one VietSuperSpeech recording."""

    audio_path: str
    reference: str
    hypothesis: str
    audio_seconds: float
    speech_seconds: float
    inference_seconds: float
    languages: list[str]
    asr_routes: list[str]
    word_error_rate: float
    character_error_rate: float


class AudioModels(Protocol):
    """Model access used by the API and this offline evaluation."""

    def get_vad(self) -> Any: ...

    def get_language(self) -> Any: ...

    def get_asr(self) -> Any: ...

    def get_whisper(self) -> Any: ...


def parse_args() -> argparse.Namespace:
    """Parse command-line settings for the VietSuperSpeech evaluation."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-root", type=Path, default=DEFAULT_DATASET_ROOT)
    parser.add_argument("--split", choices=("dev", "train"), default="dev")
    parser.add_argument("--limit", type=int, help="Number of manifest entries to evaluate.")
    parser.add_argument("--model", help="PhoWhisper model ID (default: PHOWHISPER_MODEL setting).")
    parser.add_argument("--whisper-model", help="Multilingual Whisper model ID (default: WHISPER_MODEL setting).")
    parser.add_argument(
        "--language-model", help="Silero Lang 95 ONNX URL or path (default: SILERO_LANG95_MODEL setting)."
    )
    parser.add_argument("--device", choices=("auto", "cpu", "cuda"), help="Override MODEL_DEVICE.")
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


def prepare_speech_regions(samples: list[ManifestSample], models: AudioModels) -> list[PreparedSample]:
    """Decode with the API's FFmpeg path and keep each VAD region separate."""
    from app.services.audio.preprocess import SAMPLE_RATE, decode_audio
    from silero_vad import get_speech_timestamps

    vad_model = models.get_vad()
    prepared: list[PreparedSample] = []
    for sample in samples:
        waveform = decode_audio(sample.audio_path.read_bytes())
        timestamps = get_speech_timestamps(torch.from_numpy(waveform), vad_model, sampling_rate=SAMPLE_RATE)
        regions = [waveform[item["start"] : item["end"]] for item in timestamps]
        regions = [region for region in regions if region.size]
        prepared.append(
            PreparedSample(
                audio_path=sample.audio_path,
                reference=sample.reference,
                audio_seconds=len(waveform) / SAMPLE_RATE,
                speech_seconds=sum(len(region) for region in regions) / SAMPLE_RATE,
                sampling_rate=SAMPLE_RATE,
                speech_regions=regions,
            )
        )
    return prepared


def transcribe_prepared_samples(
    samples: list[PreparedSample],
    models: AudioModels,
    *,
    clock: Callable[[], float] = perf_counter,
) -> list[RoutedTranscription]:
    """Detect language and transcribe each region using the API's routing rule."""
    results: list[RoutedTranscription] = []
    for sample in samples:
        texts: list[str] = []
        languages: list[str] = []
        asr_routes: list[str] = []
        inference_seconds = 0.0
        for region in sample.speech_regions:
            detector = models.get_language()
            started_at = clock()
            language = detector(torch.from_numpy(region))
            inference_seconds += clock() - started_at
            asr_input = {"array": region, "sampling_rate": sample.sampling_rate}
            if language == "vi":
                asr = models.get_asr()
                asr_routes.append("phowhisper")
                started_at = clock()
                prediction = asr(asr_input)
            else:
                asr = models.get_whisper()
                asr_routes.append("whisper")
                started_at = clock()
                prediction = asr(asr_input, generate_kwargs={"task": "transcribe"})
            inference_seconds += clock() - started_at
            texts.append(str(prediction["text"]).strip())
            languages.append(language)
        results.append(
            RoutedTranscription(" ".join(text for text in texts if text), languages, asr_routes, inference_seconds)
        )
    return results


def main() -> None:
    """Run the API audio-to-ASR stages and write a VietSuperSpeech report."""
    from app.core.audio_models import ModelRegistry
    from app.core.config import Settings

    args = parse_args()
    samples = load_manifest_samples(args.dataset_root, args.split, args.limit)
    settings = Settings.from_environment()
    overrides = {
        key: value
        for key, value in {
            "phowhisper_model": args.model,
            "whisper_model": args.whisper_model,
            "silero_lang95_model": args.language_model,
            "model_device": args.device,
        }.items()
        if value is not None
    }
    settings = replace(settings, **overrides)
    models = ModelRegistry(settings)
    prepared = prepare_speech_regions(samples, models)
    transcriptions = transcribe_prepared_samples(prepared, models)
    total_inference_seconds = sum(item.inference_seconds for item in transcriptions)
    results = [
        RoutedSampleResult(
            audio_path=str(sample.audio_path),
            reference=sample.reference,
            hypothesis=transcription.hypothesis,
            audio_seconds=sample.audio_seconds,
            speech_seconds=sample.speech_seconds,
            inference_seconds=transcription.inference_seconds,
            languages=transcription.languages,
            asr_routes=transcription.asr_routes,
            word_error_rate=error_rate(sample.reference, transcription.hypothesis, character_level=False),
            character_error_rate=error_rate(sample.reference, transcription.hypothesis, character_level=True),
        )
        for sample, transcription in zip(prepared, transcriptions, strict=True)
    ]
    report = {
        "dataset_root": str(args.dataset_root),
        "split": args.split,
        "manifest_sha256": sha256((args.dataset_root / f"{args.split}.json").read_bytes()).hexdigest(),
        "sample_count": len(results),
        "model": settings.phowhisper_model,
        "whisper_model": settings.whisper_model,
        "language_model": settings.silero_lang95_model,
        "model_device": settings.model_device,
        "asr_device": "cuda:0" if settings.model_device != "cpu" and torch.cuda.is_available() else "cpu",
        "language_provider": models.get_language().model.get_providers()[0]
        if any(sample.speech_regions for sample in prepared)
        else None,
        "preprocessing": "FFmpeg 16 kHz mono float32; Silero VAD; per-region language routing",
        "timing_scope": "Language detection and ASR inference; excludes decoding, VAD, and model loading",
        "dependency_versions": {
            name: version(name) for name in ("torch", "transformers", "onnxruntime", "silero-vad")
        },
        "samples": [asdict(result) for result in results],
        "total_inference_seconds": total_inference_seconds,
        "mean_inference_seconds": total_inference_seconds / len(results),
        "mean_wer": sum(result.word_error_rate for result in results if result.word_error_rate is not None) / len(results),
        "mean_cer": sum(result.character_error_rate for result in results if result.character_error_rate is not None) / len(results),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Report: {args.output}; samples: {len(results)}; mean WER: {report['mean_wer']:.4f}; mean CER: {report['mean_cer']:.4f}")


if __name__ == "__main__":
    main()
