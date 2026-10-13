"""Fine-tune PhoWhisper using VietSuperSpeech train.json and dev.json.

Run from the repository root:
    uv run python -m ai_training.training.asr.train \
        --dataset-root /kaggle/input/<dataset-folder>/VietSuperSpeech \
        --output-dir /kaggle/working/phowhisper-vietsuperspeech
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
from dataclasses import asdict, dataclass
from pathlib import Path
from time import perf_counter
from typing import Any

import numpy as np
import torch
import torchaudio
import transformers
from torch.utils.data import DataLoader, Dataset
from transformers import WhisperForConditionalGeneration, WhisperProcessor, get_linear_schedule_with_warmup

from ai_training.evaluation.evaluate_vietsuperspeech_silero_phowhisper import (
    DEFAULT_DATASET_ROOT,
    ManifestSample,
    load_manifest_samples,
)
from ai_training.evaluation.test_vivos_silero_phowhisper import error_rate


SAMPLE_RATE = 16_000
MAX_AUDIO_SECONDS = 30


@dataclass(frozen=True)
class TrainingConfig:
    dataset_root: Path
    output_dir: Path
    model: str
    model_revision: str
    epochs: int
    batch_size: int
    gradient_accumulation: int
    learning_rate: float
    seed: int
    train_limit: int | None
    dev_limit: int | None
    device: str


def parse_args() -> TrainingConfig:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-root", type=Path, default=DEFAULT_DATASET_ROOT)
    parser.add_argument(
        "--output-dir", type=Path, default=Path("ai_training/results/phowhisper-vietsuperspeech")
    )
    parser.add_argument("--model", default="vinai/PhoWhisper-base")
    parser.add_argument("--model-revision", default="main")
    parser.add_argument("--epochs", type=int, default=3)
    parser.add_argument("--batch-size", type=int, default=4)
    parser.add_argument("--gradient-accumulation", type=int, default=4)
    parser.add_argument("--learning-rate", type=float, default=1e-5)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--train-limit", type=int)
    parser.add_argument("--dev-limit", type=int)
    parser.add_argument("--device", choices=("auto", "cpu", "cuda"), default="auto")
    args = parser.parse_args()
    for name in ("epochs", "batch_size", "gradient_accumulation", "train_limit", "dev_limit"):
        value = getattr(args, name)
        if value is not None and value < 1:
            parser.error(f"--{name.replace('_', '-')} must be at least 1")
    if args.learning_rate <= 0:
        parser.error("--learning-rate must be positive")
    return TrainingConfig(**vars(args))


class VietSuperSpeechDataset(Dataset[dict[str, Any]]):
    """Decode manifest WAVs and prepare Whisper features on demand."""

    def __init__(
        self, samples: list[ManifestSample], processor: WhisperProcessor, max_label_tokens: int
    ) -> None:
        self.samples = samples
        self.processor = processor
        self.max_label_tokens = max_label_tokens

    def __len__(self) -> int:
        return len(self.samples)

    def __getitem__(self, index: int) -> dict[str, Any]:
        sample = self.samples[index]
        waveform, sampling_rate = torchaudio.load(sample.audio_path)
        waveform = waveform.mean(dim=0)
        if sampling_rate != SAMPLE_RATE:
            waveform = torchaudio.functional.resample(waveform, sampling_rate, SAMPLE_RATE)
        if waveform.numel() == 0 or waveform.numel() > MAX_AUDIO_SECONDS * SAMPLE_RATE:
            raise ValueError(f"Audio must contain at most {MAX_AUDIO_SECONDS} seconds: {sample.audio_path}")

        input_features = self.processor.feature_extractor(
            waveform.numpy(), sampling_rate=SAMPLE_RATE
        ).input_features[0]
        labels = self.processor.tokenizer(sample.reference).input_ids
        if len(labels) > self.max_label_tokens:
            raise ValueError(f"Transcript exceeds model token limit: {sample.audio_path}")
        return {"input_features": input_features, "labels": labels, "reference": sample.reference}


def collate_samples(samples: list[dict[str, Any]], processor: WhisperProcessor) -> dict[str, Any]:
    inputs = processor.feature_extractor.pad(
        [{"input_features": sample["input_features"]} for sample in samples], return_tensors="pt"
    )
    labels = processor.tokenizer.pad(
        [{"input_ids": sample["labels"]} for sample in samples], return_tensors="pt"
    )
    label_ids = labels["input_ids"].masked_fill(labels["attention_mask"].ne(1), -100)
    if torch.all(label_ids[:, 0] == processor.tokenizer.bos_token_id):
        label_ids = label_ids[:, 1:]
    inputs["labels"] = label_ids
    inputs["references"] = [sample["reference"] for sample in samples]
    return inputs


def evaluate(
    model: WhisperForConditionalGeneration,
    processor: WhisperProcessor,
    loader: DataLoader[dict[str, Any]],
    device: torch.device,
) -> dict[str, float]:
    model.eval()
    word_errors: list[float] = []
    character_errors: list[float] = []
    started_at = perf_counter()
    with torch.inference_mode():
        for batch in loader:
            references = batch.pop("references")
            features = batch["input_features"].to(device)
            predictions = model.generate(features, language="vietnamese", task="transcribe")
            hypotheses = processor.batch_decode(predictions, skip_special_tokens=True)
            for reference, hypothesis in zip(references, hypotheses, strict=True):
                word_errors.append(error_rate(reference, hypothesis, character_level=False))
                character_errors.append(error_rate(reference, hypothesis, character_level=True))
    return {
        "wer": sum(word_errors) / len(word_errors),
        "cer": sum(character_errors) / len(character_errors),
        "inference_seconds": perf_counter() - started_at,
    }


def train_epoch(
    model: WhisperForConditionalGeneration,
    loader: DataLoader[dict[str, Any]],
    optimizer: torch.optim.Optimizer,
    scheduler: Any,
    scaler: torch.amp.GradScaler,
    device: torch.device,
    gradient_accumulation: int,
) -> float:
    model.train()
    optimizer.zero_grad(set_to_none=True)
    total_loss = 0.0
    for batch_index, batch in enumerate(loader):
        batch.pop("references")
        inputs = {name: value.to(device) for name, value in batch.items()}
        group_start = (batch_index // gradient_accumulation) * gradient_accumulation
        group_size = min(gradient_accumulation, len(loader) - group_start)
        with torch.autocast(device_type=device.type, enabled=device.type == "cuda"):
            loss = model(**inputs).loss
        total_loss += loss.detach().item()
        scaler.scale(loss / group_size).backward()
        if (batch_index + 1) % gradient_accumulation == 0 or batch_index + 1 == len(loader):
            scaler.step(optimizer)
            scaler.update()
            scheduler.step()
            optimizer.zero_grad(set_to_none=True)
    return total_loss / len(loader)


def manifest_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    config = parse_args()
    train_samples = load_manifest_samples(config.dataset_root, "train", config.train_limit)
    dev_samples = load_manifest_samples(config.dataset_root, "dev", config.dev_limit)
    if config.device == "cuda" and not torch.cuda.is_available():
        raise RuntimeError("CUDA was requested but is unavailable")
    device = torch.device("cuda" if config.device != "cpu" and torch.cuda.is_available() else "cpu")

    random.seed(config.seed)
    np.random.seed(config.seed)
    torch.manual_seed(config.seed)
    if device.type == "cuda":
        torch.cuda.manual_seed_all(config.seed)

    processor = WhisperProcessor.from_pretrained(
        config.model, revision=config.model_revision, language="vietnamese", task="transcribe"
    )
    model = WhisperForConditionalGeneration.from_pretrained(
        config.model, revision=config.model_revision
    ).to(device)
    model.config.use_cache = False
    train_loader = DataLoader(
        VietSuperSpeechDataset(train_samples, processor, model.config.max_target_positions),
        batch_size=config.batch_size,
        shuffle=True,
        collate_fn=lambda samples: collate_samples(samples, processor),
    )
    dev_loader = DataLoader(
        VietSuperSpeechDataset(dev_samples, processor, model.config.max_target_positions),
        batch_size=config.batch_size,
        collate_fn=lambda samples: collate_samples(samples, processor),
    )
    optimizer = torch.optim.AdamW(model.parameters(), lr=config.learning_rate)
    total_steps = config.epochs * math.ceil(len(train_loader) / config.gradient_accumulation)
    scheduler = get_linear_schedule_with_warmup(
        optimizer, num_warmup_steps=min(100, total_steps // 10), num_training_steps=total_steps
    )
    scaler = torch.amp.GradScaler("cuda", enabled=device.type == "cuda")

    config.output_dir.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {
        "config": {
            **asdict(config),
            "dataset_root": str(config.dataset_root),
            "output_dir": str(config.output_dir),
        },
        "model_commit": getattr(model.config, "_commit_hash", None),
        "train_manifest_sha256": manifest_hash(config.dataset_root / "train.json"),
        "dev_manifest_sha256": manifest_hash(config.dataset_root / "dev.json"),
        "train_samples": len(train_samples),
        "dev_samples": len(dev_samples),
        "sample_rate": SAMPLE_RATE,
        "max_audio_seconds": MAX_AUDIO_SECONDS,
        "device": str(device),
        "torch_version": torch.__version__,
        "torchaudio_version": torchaudio.__version__,
        "transformers_version": transformers.__version__,
        "epochs": [],
    }
    baseline = evaluate(model, processor, dev_loader, device)
    report["baseline"] = baseline
    print(f"Baseline: WER={baseline['wer']:.4f}, CER={baseline['cer']:.4f}", flush=True)
    best_wer = float("inf")
    for epoch in range(1, config.epochs + 1):
        started_at = perf_counter()
        train_loss = train_epoch(
            model, train_loader, optimizer, scheduler, scaler, device, config.gradient_accumulation
        )
        metrics = evaluate(model, processor, dev_loader, device)
        epoch_report = {
            "epoch": epoch,
            "train_loss": train_loss,
            **metrics,
            "total_seconds": perf_counter() - started_at,
        }
        report["epochs"].append(epoch_report)
        print(
            f"Epoch {epoch}: loss={train_loss:.4f}, WER={metrics['wer']:.4f}, CER={metrics['cer']:.4f}",
            flush=True,
        )
        if metrics["wer"] < best_wer:
            best_wer = metrics["wer"]
            model.config.use_cache = True
            model.save_pretrained(config.output_dir / "model")
            processor.save_pretrained(config.output_dir / "model")
            model.config.use_cache = False
            report["best_epoch"] = epoch
        (config.output_dir / "training_report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )


if __name__ == "__main__":
    main()
