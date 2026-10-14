# Audio language routing evaluation note

## Change

The API now classifies each VAD speech region with Silero Lang 95. Its top
language prediction selects PhoWhisper for Vietnamese and multilingual OpenAI
Whisper for all other languages. The classifier reads up to 20 seconds from
the start of each region. FFmpeg decoding, VAD thresholds, speaker assignment,
and response fields are unchanged.

## Reproducibility

- Language model: Silero Lang 95 archived ONNX checkpoint mirrored at
  `Derur/silero-models` revision `ab3dfc803ab6384ac507dc81f0d46fd1613058b8`.
  The Vietnamese class index (45) comes from
  `snakers4/silero-vad:v3.1/files/lang_dict_95.json`. The mirrored weight
  SHA-256 is `12a91f706ad6ca5a5eba4e84a96739d077e56f121ec71db86a752f62e218a227`.
  Set `SILERO_LANG95_MODEL` to a local ONNX file for offline deployment.
- ASR defaults: `vinai/PhoWhisper-base` for Vietnamese and
  `openai/whisper-base` for other languages. No immutable model revisions are
  configured.
- Preprocessing: FFmpeg 16 kHz mono float32; existing Silero VAD segmentation.
  The language model receives at most the first 20 seconds of each VAD region.
- Device: `MODEL_DEVICE` (`auto`, `cpu`, or `cuda`); `auto` uses CUDA when
  available. Model and dependency versions are resolved by `uv.lock`.
- Dataset, split, sample count, and random seed: no model evaluation was run.

The pinned ONNX checkpoint was downloaded and loaded on CPU, and one synthetic
one-second silent tensor passed through it. This only verifies runtime loading
and tensor shape handling; it is not a quality or latency measurement.

## Comparison still required

Baseline and candidate WER/CER, Vietnamese routing accuracy, other-language
routing accuracy, diarization DER/JER, latency, and memory use have not been
measured. Test both branches with held-out multilingual meeting audio before
relying on the change in production. Short speech regions, mixed languages
within one VAD region, and unsupported or mistaken Lang 95 predictions may
send speech to the wrong ASR model. The repository's fast API tests verify
routing and response behavior with fake inference models, not recognition
quality.

The VietSuperSpeech evaluation now runs the API's per-region language routing
and reports WER/CER on the selected manifest split. It records the manifest
checksum, model choices, per-region routes, and inference time. No real
VietSuperSpeech evaluation was run as part of this change because the dataset
is not present in the workspace.
