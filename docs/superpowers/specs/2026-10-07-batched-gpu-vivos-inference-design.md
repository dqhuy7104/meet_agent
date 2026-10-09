# Batched GPU VIVOS inference

## Goal

Run PhoWhisper transcription in batches after Silero VAD, using a GPU when CUDA
is available. Preserve current evaluation metrics and report ordering.

## Interface

Add `--batch-size` to the evaluator. It defaults to `5` and must be at least
one.

## Execution model

1. Load Silero VAD once, then decode and VAD-process each selected WAV file.
2. Retain per-file audio duration, speech duration, path, and reference.
3. Exclude inputs with no detected speech from ASR and assign them an empty
   transcript.
4. When CUDA is available, load the ASR pipeline on `device=0` and transcribe
   the remaining VAD outputs in batches of `--batch-size`.
5. When CUDA is unavailable, load it on CPU (`device=-1`) and force an
   effective ASR batch size of one, regardless of the supplied argument.
6. Reattach ASR outputs to their original samples and write the existing JSON
   report format in deterministic input order.

## Inference timing

Measure only PhoWhisper ASR calls with `time.perf_counter()`. Do not include
model loading, WAV decoding, or Silero VAD. Each batch duration is split evenly
among its speech-bearing samples and stored as `inference_seconds`; samples
without speech receive `0.0`. Add `total_inference_seconds` to the top-level
report as the sum of actual ASR batch durations.

## Errors and observability

Reject batch sizes below one. Print the selected device and effective batch
size before inference. Existing decode, model-loading, and inference failures
should propagate with their original context.

## Validation

Test argument validation, CPU batch-size forcing, GPU batch-size propagation,
empty-speech handling, stable sample/result ordering, per-sample timing
allocation, and aggregate ASR timing with mocked model dependencies.
