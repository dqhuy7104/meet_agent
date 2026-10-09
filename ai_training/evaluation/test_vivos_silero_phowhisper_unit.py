"""Regression tests for batched ASR orchestration without loading ML models."""

from pathlib import Path
import unittest

from ai_training.evaluation.test_vivos_silero_phowhisper import (
    PreparedSample,
    effective_batch_size,
    transcribe_prepared_samples,
)


class FakeAsrPipeline:
    """Minimal pipeline double that records batching inputs."""

    def __init__(self) -> None:
        self.batch_size: int | None = None
        self.inputs: list[dict[str, object]] = []

    def __call__(self, inputs: list[dict[str, object]], *, batch_size: int) -> list[dict[str, str]]:
        self.batch_size = batch_size
        self.inputs = inputs
        return [{"text": f"transcript {index}"} for index, _ in enumerate(inputs, start=1)]


class BatchedAsrTest(unittest.TestCase):
    """Verify device-specific batching and output alignment."""

    def test_cpu_forces_single_item_batches(self) -> None:
        self.assertEqual(effective_batch_size(5, use_cuda=False), 1)

    def test_cuda_uses_requested_batch_size(self) -> None:
        self.assertEqual(effective_batch_size(5, use_cuda=True), 5)

    def test_invalid_batch_size_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            effective_batch_size(0, use_cuda=True)

    def test_transcripts_and_timing_keep_input_order_and_skip_silence(self) -> None:
        samples = [
            PreparedSample(Path("silent.wav"), None, 1.0, 0.0, None),
            PreparedSample(Path("first.wav"), None, 1.0, 0.4, [0.1]),
            PreparedSample(Path("second.wav"), None, 1.0, 0.5, [0.2]),
        ]
        pipeline = FakeAsrPipeline()
        timestamps = iter((10.0, 10.5, 20.0, 21.5))

        transcripts, sample_seconds, total_seconds = transcribe_prepared_samples(
            samples, pipeline, batch_size=1, clock=lambda: next(timestamps)
        )

        self.assertEqual(transcripts, ["", "transcript 1", "transcript 1"])
        self.assertEqual(sample_seconds, [0.0, 0.5, 1.5])
        self.assertEqual(total_seconds, 2.0)
        self.assertEqual(pipeline.batch_size, 1)
        self.assertEqual(len(pipeline.inputs), 1)


if __name__ == "__main__":
    unittest.main()
