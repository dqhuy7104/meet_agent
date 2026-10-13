# Meeting AI

Monorepo for the Meeting AI web client, API, and ML workflows.

## Audio processing API

The API accepts an uploaded audio file and returns VAD speech regions,
PhoWhisper transcription, and pyannote speaker turns. Model weights are loaded
when the first audio request needs them. FFmpeg must be installed on the host.

```bash
uv sync
cp -n .env.example .env
# Set HUGGINGFACE_TOKEN in .env after accepting the pyannote model agreement.
PYTHONPATH=apps/api uv run --env-file .env uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Accept the `pyannote/speaker-diarization-community-1` model agreement on
Hugging Face before the first request. Send an audio file (at most 25 MiB and
30 minutes):

```bash
curl -X POST http://localhost:8000/api/audio/process \
  -F "file=@meeting.wav"
```

`transcript_segments` are the VAD regions transcribed by PhoWhisper. Each
region is assigned to the pyannote speaker with the greatest time overlap;
`diarization_segments` preserve all speaker turns, including any overlaps.

`.env` is ignored by Git. Shell environment variables take precedence over
values in `.env`; restart the API after changing model settings. Other scripts
that need the token can also run through `uv run --env-file .env`, for example:

```bash
uv run --env-file .env python ai_training/evaluation/test_viyt_vad_pyannote.py
```

## PhoWhisper fine-tuning

Mount or download VietSuperSpeech so `--dataset-root` contains `train.json`,
`dev.json`, and `audio/`. From the repository root, run:

```bash
uv run python -m ai_training.training.asr.train \
  --dataset-root /path/to/VietSuperSpeech \
  --output-dir ai_training/results/phowhisper-vietsuperspeech
```

On Kaggle, use the dataset's actual path under `/kaggle/input/` and set
`--output-dir` under `/kaggle/working/`. The script evaluates the base model
and each epoch on the held-out `dev.json` split, then saves the best epoch's
model and `training_report.json`. WER and CER are means of per-sample error
rates, matching the existing evaluation script. Use `--train-limit` and
`--dev-limit` for a small trial; those results are not full-dataset metrics.
Dataset transcripts may be machine-generated, so review labels before relying
on quality scores.
