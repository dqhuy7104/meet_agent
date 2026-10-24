# Meeting AI

Local meeting software for audio upload and Google Meet recording, speech recognition, speaker diarization, a private meeting library, and optional RAG search.

## Components

- **FastAPI:** audio processing and meeting APIs.
- **Worker:** background meeting processing.
- **React/Vite:** local web UI at `/app/`.
- **PostgreSQL + pgvector:** metadata, transcripts, jobs, and the optional RAG index. Audio remains on disk.

FFmpeg converts audio to 16 kHz mono. Silero VAD finds speech, Silero Lang 95 routes Vietnamese to PhoWhisper and other languages to Whisper, and pyannote diarizes speakers. Models are loaded lazily per process.

## Requirements

- Python 3.10+, [uv](https://docs.astral.sh/uv/), and FFmpeg
- Docker Engine and Compose for the full local stack
- A `HUGGINGFACE_TOKEN` with access to `pyannote/speaker-diarization-community-1`
- Chrome and PulseAudio/PipeWire (`pactl`) for the Google Meet bot

## Quick start (Docker, Linux)

```bash
cp -n .env.example .env
docker compose up -d --build
docker compose ps
```

Set `HUGGINGFACE_TOKEN`, `POSTGRES_PASSWORD`, and the matching `DATABASE_URL` in `.env`. Open `http://127.0.0.1:5173/app/`; the API is at `http://127.0.0.1:8000`.

The first inference downloads models. Docker uses CPU. Use `docker compose logs --tail=100 api worker migrate` for diagnostics. `docker compose down` preserves volumes; do not use `down -v` for upgrades. The Meet bot runs on the host, not in Compose.

## Run on the host

```bash
uv sync
cp -n .env.example .env
docker compose up -d database
PYTHONPATH=apps/api uv run --env-file .env python -m app.db.migrate
PYTHONPATH=apps/api uv run --env-file .env uvicorn app.main:app --host 127.0.0.1 --port 8000 --no-access-log
PYTHONPATH=apps/api uv run --env-file .env python -m app.services.meetings.worker
```

Start the API and worker in separate terminals. For web development:

```bash
cd apps/web
npm install
npm run dev
```

Vite proxies `/api` to the local API. Use `npm run typecheck`, `npm test`, or `npm run build` for web checks.

## API

### Process one audio file

```bash
curl -X POST http://localhost:8000/api/audio/process -F "file=@meeting.wav"
```

This stateless endpoint accepts files up to 25 MiB and 30 minutes. `transcript_segments` are transcribed VAD regions, assigned to the speaker with the greatest overlap. `diarization_segments` retain all speaker turns, including overlaps.

### Meeting library

Meeting routes are loopback-only and single-user. Do not expose them through a public proxy.

| Request | Result |
| --- | --- |
| `POST /api/meetings` | Queue a legacy `file` or ordered `files`, with optional `title`. |
| `GET /api/meetings`, `GET /api/meetings/{id}` | List meetings or retrieve completed details. |
| `GET /api/meetings/{id}/audio[/{ordinal}]` | Stream original audio with byte ranges. |
| `GET /api/meetings/search?q=...` | Search completed transcripts. |
| `POST /api/meetings/{id}/retry` | Requeue a failed meeting. |
| `DELETE /api/meetings/{id}` | Cancel processing and delete audio/results while retaining metadata. |
| `POST /api/chat/meetings/{id}/summary` | Summarize one completed meeting transcript in Vietnamese with the first configured RAG chat provider/model. |

Each file is limited to 25 MiB. A meeting supports up to 10 files, 100 MiB total, and 30 decoded minutes. Files share one timeline; speakers from later files are prefixed by part because identity cannot be proven across recordings.

The completed meeting page counts distinct diarization speaker labels, not speaker turns. These labels are not verified identities. It automatically requests a summary when speech exists. The summary endpoint requires `RAG_ENABLED=true` and a chat model in `RAG_PROVIDERS_JSON`, but does not require an embedding profile or index. It reads the stored transcript, makes bounded LLM requests, and returns `summary`, `provider_id`, `model_id`, and `prompt_version`; summaries are not stored. If the configured provider is external, the existing `external_data_enabled` opt-in governs sending transcript content. Provider failures can be retried from the page.

## Configuration and data

`.env` is Git-ignored and shell variables override it.

| Variable | Purpose |
| --- | --- |
| `HUGGINGFACE_TOKEN` | Hugging Face authentication for pyannote. |
| `PHOWHISPER_MODEL`, `WHISPER_MODEL`, `PYANNOTE_MODEL`, `MODEL_DEVICE` | Model and device settings. |
| `DATABASE_URL`, `POSTGRES_*` | Shared PostgreSQL configuration. |
| `MEETING_DATA_DIR` | Private audio, bot profile, and temporary files. |
| `BOT_CHROME_PATH`, `BOT_AUDIO_SINK` | Google Meet bot settings. |
| `RAG_ENABLED`, `RAG_PROVIDERS_JSON`, `RAG_EMBEDDING_PROFILES_JSON` | Optional RAG configuration. |

PostgreSQL stores meeting state and transcripts; `MEETING_DATA_DIR` stores private audio. Keep both private and back them up together after stopping API, workers, and the bot. Requests never run migrations; run `app.db.migrate` explicitly.

## Google Meet bot

Create a dedicated audio sink:

```bash
pactl load-module module-null-sink sink_name=meet_bot sink_properties=device.description=Meeting_AI_Bot
pactl list short sinks
pactl list short sources
```

Set `BOT_AUDIO_SINK=meet_bot`, `BOT_CHROME_PATH`, and an absolute `MEETING_DATA_DIR` when needed. Sign in once with a dedicated bot account using the Chrome profile in `<MEETING_DATA_DIR>/bot-profile`, then close Chrome.

```bash
PYTHONPATH=apps/api uv run --env-file .env python -m app.services.meetings.bot_runner
```

Enter a `https://meet.google.com/...` link in the web app and inform participants that recording is active. The host may need to admit **Meeting AI Recorder**. The bot records mono 16 kHz WAV only after admission, rolls over before 30 minutes, and queues closed parts for processing. It does not rejoin automatically. Bot controls are under `/api/bot-sessions`; Meet URLs are never returned.

## RAG (optional)

RAG uses the same PostgreSQL/pgvector database. Enable `RAG_ENABLED=true`, configure allowlisted providers and immutable embedding profiles, then run one worker:

```bash
PYTHONPATH=apps/api uv run --env-file .env python -m app.services.rag.worker --migrate --backfill
```

Use a new profile ID and rebuild when model revision, embedding dimensions, or chunking changes. Enable external providers only with approval to send production transcripts. Use `GET /api/chat/providers` and `POST /api/chat/queries`; unsupported answers return `insufficient_evidence`.

## Tests and ML

```bash
PYTHONPATH=apps/api uv run python -m unittest discover -s apps/api/tests -p 'test_*.py'
uv run python -m unittest discover -s ai_training/evaluation -p '*_unit.py'
uv run python ai_training/evaluation/evaluate_vietsuperspeech_silero_phowhisper.py \
  --dataset-root /path/to/VietSuperSpeech --limit 5
```

Set `TEST_DATABASE_URL` to a disposable database for PostgreSQL test coverage. Real model evaluation needs datasets, downloaded models, and a token for pyannote. See `ai_training/` for fine-tuning and VIVOS, VietSuperSpeech, and ViYT-Diar evaluations; a limited run is only a smoke test.

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

## Local voice identification (opt-in)

Voice profiles and manual speaker corrections are local-only. Create a profile
on `/app/voice-profiles`, then enroll a separate upload or a speaker from a
completed meeting. Uploads must contain one speaker, at least 10 seconds of
exclusive speech, at most two minutes and 25 MiB. The shared meeting worker
processes enrollment jobs. Each profile retains five samples in successful
insertion order: a sixth successful enrollment evicts the oldest; a failed job
evicts nothing. Corrections affect only that meeting; enrollment is explicit.
Existing completed meetings are not automatically reidentified.

Before starting API and worker, explicitly apply shared PostgreSQL migrations:

```bash
PYTHONPATH=apps/api uv run --env-file .env python -m app.db.migrate
```

This includes `002_voice_identification.sql` (profiles, samples, jobs and
identities) and `003_voice_enrollment_lifecycle.sql` (enrollment recovery and
cancellation). Requests never run migrations. PostgreSQL via `DATABASE_URL`
owns meeting, bot, outbox, voice and RAG state; `MEETING_DATA_DIR` holds private
audio. Older SQLite setup/backup descriptions above are historical and do not
apply to this PostgreSQL deployment.

Enrollment and matching reuse the configured pyannote pipeline. For a remote
pipeline set `PYANNOTE_REVISION` to its immutable 40-character Hugging Face
commit; a branch or tag is insufficient. Local fingerprints hash the pipeline
configuration and embedding checkpoint. Fingerprints also include pyannote.audio
version and 16 kHz mono float32 preprocessing. Samples from another fingerprint
stay visible/deletable but cannot match the current model. Automatic matching
remains off by default (`VOICE_IDENTIFICATION_ENABLED=false`); no production
similarity defaults or real accuracy/latency measurements are supplied.

After obtaining approval for a held-out dataset, prepare a private CSV with
`audio_path,person_id,session_id,role`. Roles are `enrollment` or `probe`;
relative paths resolve beside the CSV. Include known probes and unknown people
without enrollment rows. Enrollment and probe sessions for each person must
differ. Enrollment row order defines FIFO; only the last five per person are
retained. All clips use the production upload extraction requirements above.
Run locally with explicit candidate grids (values below are illustrative,
not recommended thresholds):

```bash
uv run --env-file .env python -m ai_training.evaluation.evaluate_voice_identification \
  --manifest /private/held-out.csv --dataset-version approved-heldout-v1 \
  --thresholds 0.7 0.8 0.9 --margins 0.05 0.1 0.2 \
  --output ai_training/results/voice-calibration.json
```

The report contains counts, fingerprint, model revision, dependencies,
preprocessing, candidate grids, FAR/FRR, unknown rate, correct identification
rate, and extraction latency including cold loading. It omits audio paths and
content. FAR measures accepted impostor trials (unknown probes or a wrong top
identity) divided by impostor trials; FRR measures rejected known probes;
unknown rate measures all rejected probes. The selected pair maximizes correct
identification of known probes among candidates with empirical FAR <=1%; ties
retain grid order. No valid candidate produces null calibration: keep automatic
identification disabled. Small datasets do not establish a statistical 1% error
guarantee. Record baseline/candidate results, latency/cost and known regressions
for the exact fingerprint before setting both `VOICE_MATCH_MIN_COSINE` and
`VOICE_MATCH_MIN_MARGIN` and enabling identification. Preserve a separate final
test split for an unbiased quality result after calibration. No approved dataset
is supplied and no real-model quality or latency result is claimed.

Voiceprints are sensitive personal data. Keep manifests, reports, recordings,
vectors and provider tokens out of Git and logs; do not send them to external
providers. Uploaded enrollment audio is deleted after success/failure/cancellation;
embeddings persist until FIFO eviction or explicit sample/profile deletion.
Deleting a profile clears its identity mappings; deleting/cancelling a source
meeting removes its enrolled samples without revising assignments in other
meetings. Stop API, worker, bot and RAG processes before a coordinated backup of
PostgreSQL (`pg_dump`) and the private data root. Apply the same access and
retention controls to backups; deleting live samples does not erase backups.
Rollback by setting `VOICE_IDENTIFICATION_ENABLED=false`, restarting processes,
and reverting use of the additive API/UI fields. Keep additive tables intact;
remove biometric data explicitly through sample/profile deletion when desired.
