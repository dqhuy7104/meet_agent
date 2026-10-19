# Meeting AI

Monorepo for the Meeting AI web client, API, and ML workflows.

## Audio processing API

The API accepts an uploaded audio file and returns VAD speech regions,
language-routed transcription, and pyannote speaker turns. Silero Lang 95
classifies each VAD region: Vietnamese uses PhoWhisper; other languages use
the local multilingual Whisper model selected by `WHISPER_MODEL`. Model weights
are loaded when the first audio request needs them. FFmpeg must be installed
on the host.

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

`transcript_segments` are the transcribed VAD regions. Each
region is assigned to the pyannote speaker with the greatest time overlap;
`diarization_segments` preserve all speaker turns, including any overlaps.
The response fields and timestamps are unchanged. Silence skips language
detection and ASR. Lang 95 uses at most the first 20 seconds of each speech
region and routes by its top language prediction, so a language switch inside
one region is not detected. The archived Lang 95 ONNX weights download on first
use; set `SILERO_LANG95_MODEL` to a local ONNX file to avoid that download.

`.env` is ignored by Git. Shell environment variables take precedence over
values in `.env`; restart the API after changing model settings. Other scripts
that need the token can also run through `uv run --env-file .env`, for example:

```bash
uv run --env-file .env python ai_training/evaluation/test_viyt_vad_pyannote.py
```

## Local meeting library

## Web client

The frontend in `apps/web` is a React/Vite client for the local meeting
library. It displays only data returned by the local meeting API: uploaded
meetings, processing state, original audio, transcript segments, and speaker
turns. Upload uses the local `POST /api/meetings` endpoint when the API is
running.

```bash
cd apps/web
npm install
npm run dev
```

Open the URL printed by Vite (normally `http://localhost:5173/app/`). For a
production bundle run `npm run build`. Vite proxies `/api` to the local
API while developing, so the typed client can use the local meeting routes.
`dist/` is generated and not source data. The client does not place audio or
transcript content in browser storage.

The meeting library stores uploaded audio and transcripts on this machine.
It is single-user, has no login, and must not be exposed through a proxy.
Set `MEETING_DATA_DIR` in `.env` (default `./meeting-data`). FFmpeg is required.
On a POSIX host, start the API and one worker in separate terminals:

```bash
uv sync
PYTHONPATH=apps/api uv run --env-file .env uvicorn app.main:app --host 127.0.0.1 --port 8000 --no-access-log
PYTHONPATH=apps/api uv run --env-file .env python -m app.services.meetings.worker
```

The new `/api/meetings` routes require a loopback client and local Host; browser
write requests also require an HTTP origin on the local machine. This supports
the Vite development proxy while rejecting non-local web origins. Keep access
logging disabled to avoid recording private search queries. The original
`POST /api/audio/process` still returns an immediate response and stores
nothing. The library API provides:

| Request | Result |
| --- | --- |
| `POST /api/meetings` | Multipart `file` (legacy) or ordered `files` fields and optional `title`; queues one meeting and returns 202. Optional `Idempotency-Key` safely repeats the same upload. |
| `GET /api/meetings?limit=20&offset=0` | Newest meetings and processing states. |
| `GET /api/meetings/{id}` | Metadata and completed transcript, VAD regions, and speaker turns. |
| `GET /api/meetings/{id}/audio` | First original audio; supports playback byte ranges. |
| `GET /api/meetings/{id}/audio/{ordinal}` | Original audio part by zero-based order; supports byte ranges. |
| `GET /api/meetings/search?q=...` | Transcript segment matches from completed meetings. |
| `POST /api/meetings/{id}/retry` | Requeue a failed meeting. |
| `DELETE /api/meetings/{id}` | Cancel any meeting, including a running pipeline; retain metadata and remove audio. |

Select multiple audio files in the web upload dialog, or send repeated `files`
fields in one API request. The API preserves their order and returns
`audio_files` in the meeting detail. Processing joins their transcripts and
timestamps into one timeline; speaker labels in later files are prefixed with
their part number because separate recordings cannot prove speaker identity
across files. Each file is limited to 25 MiB, a meeting to 10 files and
100 MiB total, and combined decoded audio to 30 minutes. Invalid audio
or model failure leaves the meeting in `failed` with a safe failure code;
the original audio remains available for retry or deletion. Deleting a running
meeting returns immediately, terminates its dedicated inference process, marks
the record `cancelled`, and retains filename/PID metadata while permanently
removing audio and derived results. Results and audio remain in `MEETING_DATA_DIR`
until deletion. The directory contains a SQLite
database and private audio files; keep it out of version control and protect
backups. For a consistent backup, stop both API and worker, then copy the
entire data directory. To roll back, stop both processes, preserve or export
that directory, and revert the new meeting routes and schema; the original
audio processing endpoint remains independent.

## Local Google Meet recording bot

The bot runs on the same Linux machine as the API. Install Google Chrome,
FFmpeg, and PulseAudio or PipeWire with Pulse compatibility (`pactl`). Create
a dedicated virtual output; the bot refuses to start if its sink or monitor
is missing and never records the default system output:

```bash
pactl load-module module-null-sink sink_name=meet_bot sink_properties=device.description=Meeting_AI_Bot
pactl list short sinks   # must include meet_bot
pactl list short sources # must include meet_bot.monitor
```

Set `BOT_AUDIO_SINK=meet_bot` and `BOT_CHROME_PATH` to the installed Chrome
executable in `.env`. Set `MEETING_DATA_DIR` to an absolute private path if
running the processes from different directories. The optional
`BOT_ADMISSION_TIMEOUT_SECONDS` defaults to 300. Open Chrome once with
`--user-data-dir=/absolute/path/to/meeting-data/bot-profile`, sign in to a
dedicated bot Google account, then close that Chrome window before starting
the runner. The app does not ask for or store the account password. The
profile, database, temporary files, and recordings live under
`MEETING_DATA_DIR`; the default `meeting-data/` path is Git ignored. Keep a
custom data directory outside the repository or add it to your local ignore
rules.

Run the API, bot runner, and inference worker in separate terminals:

```bash
PYTHONPATH=apps/api uv run --env-file .env uvicorn app.main:app --host 127.0.0.1 --port 8000 --no-access-log
PYTHONPATH=apps/api uv run --env-file .env python -m app.services.meetings.bot_runner
PYTHONPATH=apps/api uv run --env-file .env python -m app.services.meetings.worker
```

In the web app, paste a standard `https://meet.google.com/xxx-xxxx-xxx`
link. Inform participants that the bot records audio; Google Meet's own
recording indicator is not used. A host may need to admit **Meeting AI
Recorder**. The bot records only after admission, writes private mono 16 kHz
WAV parts, and rolls over before 30 minutes per part. A 30-minute part is
about 55 MiB, so check disk space for long meetings. Stopping the bot or a
normal meeting end queues one meeting for the existing AI worker. A browser
or capture failure reports `interrupted`; use **Xử lý phần đã ghi** to queue
the closed parts or **Xóa bản ghi** to discard them. The bot does not rejoin
automatically. Deleting the resulting meeting removes its audio and derived
results. Files remain until processed/discarded or the meeting is deleted.

Bot controls are local-only: `POST /api/bot-sessions`, `GET
/api/bot-sessions/active`, `GET /api/bot-sessions/recent`, `GET
/api/bot-sessions/{id}`, `POST /api/bot-sessions/{id}/stop`, `POST
/api/bot-sessions/{id}/process-parts`, and `DELETE /api/bot-sessions/{id}`.
Create accepts JSON `{"url":"https://meet.google.com/abc-defg-hij",
"title":"Planning"}` and an optional `Idempotency-Key`. Session responses
omit the URL. Meeting list/detail responses add `source`; detail audio parts
add `start_seconds` so gaps remain visible in the timeline. Upload limits
and `/api/audio/process` are unchanged.

Starting the API or runner adds SQLite tables and columns automatically.
For backup, stop all three processes and copy the entire data directory.
Before rollback, stop them, preserve or export that directory, and do not
run an older worker on queued bot meetings: it does not understand recordings
over 30 minutes. Test a short consented Meet, host admission, sink isolation,
rollover, stop, interruption, and deletion on the target machine before using
the bot for real meetings; browser controls depend on the installed Chrome
and current Meet interface.

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

## VietSuperSpeech ASR evaluation

Evaluate the held-out `dev.json` split with the audio API's VAD and language
routing flow:

```bash
uv run ai_training/evaluation/evaluate_vietsuperspeech_silero_phowhisper.py \
  --dataset-root /path/to/VietSuperSpeech --limit 5
```

Each VAD region is classified by Silero Lang 95, then transcribed by
PhoWhisper for Vietnamese or local multilingual Whisper otherwise. The JSON
report records per-region routes, WER/CER, inference time, model IDs, and a
manifest checksum. `--limit` is a smoke test; omit it for the full split.
Inference time covers language detection and ASR, excluding model loading,
decoding, and VAD. WER/CER remain means of per-sample error rates.
`--model`, `--whisper-model`, `--language-model`, and `--device` override the
corresponding API settings. Add `--env-file .env` after `uv run` to use local
model settings from `.env`. No diarization is run for this ASR evaluation.

## Meeting RAG (opt-in)

SQLite remains authoritative; PostgreSQL is a disposable derived index. Start
pgvector with `docker compose --env-file .env -f infra/compose.rag.yml up -d`.
Set `RAG_POSTGRES_PASSWORD` locally and `RAG_DATABASE_URL` to its loopback DSN.
The service pins pgvector 0.8.1 on PostgreSQL 17; review upgrades explicitly.
Integration tests require a dedicated `RAG_TEST_DATABASE_URL`; they create and
remove random schemas in that database, never existing meeting tables.
Stop workers before backup; use `pg_dump` for the derived database and preserve
the entire SQLite data directory separately. Roll back by disabling RAG and
stopping its worker; existing audio and meeting routes remain independent.

Run the separate index worker after configuration:
`PYTHONPATH=apps/api uv run --env-file .env python -m app.services.rag.worker --migrate --backfill`.
`--once` performs one synchronization/job iteration. Backfill also resets failed
jobs for an explicit repair attempt. Only one RAG worker may own a data directory.
Meeting completion/deletion commits a payload-free SQLite outbox event without
contacting PostgreSQL. During outages deletion still succeeds; the purge remains
pending until the worker recovers. Query-time SQLite checks exclude stale sources
before that purge completes. Startup reconciliation repairs missing publications.

Enable `RAG_ENABLED=true`, set `RAG_MAX_K` (default 10), and configure server
allowlists as JSON arrays in `.env`. Example local provider:

```dotenv
RAG_PROVIDERS_JSON='[{"id":"local","name":"Ollama","kind":"ollama","base_url":"http://127.0.0.1:11434","chat_models":[{"id":"qwen3","context_tokens":8192,"output_tokens":1024}],"embedding_models":["nomic-embed-text:latest"]}]'
RAG_EMBEDDING_PROFILES_JSON='[{"id":"local-embed-v1","provider_id":"local","model":"nomic-embed-text:latest","revision":"sha256:REPLACE_WITH_ACTUAL_DIGEST","dimensions":768,"chunk_revision":"utf8-v1"}]'
```

Get the actual digest from Ollama `GET /api/tags`; dimensions must match the
chosen model. Digest and dimension changes fail safely. Do not replace active
model tags while requests run. Profiles are immutable: change the ID and rebuild
when model revision, dimension or chunking changes. The initial cosine threshold
`RAG_MIN_COSINE_SIMILARITY=0.35` is uncalibrated; use held-out evaluation before
tuning it. Windows estimate tokens as UTF-8 bytes/3 (400 with 60 overlap); long
segments retain segment-level timing, not invented word timestamps.

For an external OpenAI-compatible server, add an entry like:

```json
{"id":"external","name":"Approved provider","kind":"openai_compatible","base_url":"https://provider.example/v1","api_key_env":"RAG_EXTERNAL_KEY","external_data_enabled":true,"chat_models":[{"id":"PINNED_CHAT_MODEL","context_tokens":8192,"output_tokens":1024}],"embedding_models":["PINNED_EMBEDDING_MODEL"]}
```

Set the key only in the server environment. External model revisions must use
provider-supported immutable model IDs: their immutability is the provider's
contract; identical-dimension silent model replacement cannot be detected
universally. Obtain administrator approval for production meeting data before
setting `external_data_enabled=true`. Indexing sends transcript chunks to the
embedding provider; queries send questions to that provider and selected context
to the chat provider. No automatic provider fallback is used.

`GET /api/chat/providers` returns safe choices and ready profiles; `POST
/api/chat/queries` accepts `question`, integer `k`, `chat_provider_id`,
`chat_model_id`, `embedding_profile_id` and optional `meeting_ids`. An empty
meeting list searches nothing. Errors use safe 422/409/503/504 codes. Requests
are bounded to 60 seconds; provider responses to 128 KiB. Use the web page
`/app/chat` to choose K/model/profile and open cited audio timestamps. This is
local-only, single-user software: keep the API on loopback and access logs off.
Sources are checked against SQLite before generation and at final validation;
a concurrent change after final validation may still race response delivery.

No credible evidence, conflicting/invalid output or missing citation IDs returns
`insufficient_evidence`. Citation checks verify IDs and claim coverage; they
cannot prove semantic entailment or prevent every hallucination. No real-provider
quality baseline is claimed. Use the held-out evaluation workflow in
`ai_training/evaluation/meeting_rag_dataset.md`, including human support ratings.
To repair an unready index, check provider availability and immutable metadata,
then restart the worker with `--backfill`; queued purges survive restarts. Run
migrations with `--migrate --once` before starting a normal worker. Run isolated
PG tests with `RAG_TEST_DATABASE_URL=<disposable-dsn> PYTHONPATH=apps/api uv run
python -m unittest discover -s apps/api/tests -p 'test_rag_postgres_integration.py'`.

The RAG worker reconnects after database outages. Embedding batch size respects
vector dimensions and the response cap. Expired jobs stop after three claims;
`--backfill` explicitly resets failures. API requests own their SQLite/PG
connections in a background thread; cancellation returns without waiting for
blocking cleanup, while that thread closes its resources and checks cancellation
before further provider work. `/api/chat/evaluation-state` exposes only local
revision/configuration metadata so evaluation can reject corpus or prompt drift.
