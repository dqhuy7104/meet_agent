# Meeting AI

## 1. Overview

Meeting AI turns meeting audio into transcripts with timestamped speaker turns. It includes a local web client, persistent meeting storage, background processing, a Google Meet bot and opt-in retrieval-augmented generation (RAG).

The stateless audio endpoint returns results without saving them. Stored meeting workflows keep metadata and transcripts in PostgreSQL and private audio on disk. Deployment currently targets a local, single-host environment.

## 2. Features

- Audio transcription with Silero VAD, language detection, PhoWhisper for Vietnamese and multilingual Whisper.
- Speaker diarization with pyannote and timestamps relative to the original audio.
- Project-based meeting libraries, transcript search, background jobs and saved summaries.
- Local accounts, project roles and shared voice profile enrollment.
- Local Google Meet recording with project-scoped access.
- Optional meeting RAG with citations and configurable model providers.
- Workspace summary and RAG system prompts managed by Admin.

Automatic voice identification is disabled by default and requires held-out calibration before enabling it.

## 3. Tech Stack

| Layer | Technologies |
| --- | --- |
| Backend | Python 3.10+, uv, FastAPI, Pydantic, psycopg |
| Audio and models | FFmpeg, PyTorch, Silero VAD/Lang 95, Transformers, PhoWhisper, Whisper, pyannote.audio |
| Frontend | React, TypeScript, Vite, React Router |
| Storage | PostgreSQL 17, pgvector 0.8.1, private audio files |
| Summaries and RAG | Ollama, optional OpenAI-compatible providers |
| Deployment and tests | Docker Compose, unittest, Vitest, Testing Library |

## 4. Architecture & Project Structure

```text
Audio upload / local Meet bot
  → FastAPI + PostgreSQL job state
  → Meeting worker: FFmpeg → VAD → language-routed ASR + diarization
  → PostgreSQL transcripts + private audio files
  → React meeting views + saved summaries
  → Optional RAG worker → pgvector index → cited chat answers
```

Models load lazily and are reused within each process. The stateless audio endpoint executes the audio pipeline directly without creating a stored meeting.

```text
apps/api/          API routes, schemas, models, services, migrations and tests
apps/web/          React/TypeScript client
ai_training/       Datasets, training scaffolds and offline evaluations
shared/python/     Shared Python scaffolds
infra/             Local deployment configuration
scripts/           Development and training utilities
pipeline.md        Current meeting/RAG flow and design notes
pyproject.toml     Python dependencies (pinned by uv.lock)
docker-compose.yml Database, migrations, API, workers and web services
```

- **FastAPI + worker:** upload, transcription, diarization, meeting processing, and RAG.
- **React:** local UI at `http://127.0.0.1:5173/app/`.
- **PostgreSQL + pgvector:** meeting metadata, transcripts, jobs, and RAG index.
- **Ollama:** local models for summaries and RAG search.

Both ASR branches enable Whisper timestamps for speech regions longer than 30 seconds.
API segment times still come from VAD. Mock tests cover this option; real-model
WER/CER and latency comparisons have not been run for this change.

The first view of a completed meeting saves its summary in PostgreSQL. Later views
read it without calling the model. Reprocessing clears the summary. Apply shared
migrations before starting the API.

Audio is kept on the local Docker volume. The API only listens on loopback; do not expose it publicly.

## 5. Prerequisites & Installation

- Docker Engine with Docker Compose
- A Hugging Face token accepted for `pyannote/speaker-diarization-community-1`
- At least 12 GB free disk space for images and local models

### Docker setup

1. Create the local configuration. Replace `YOUR_HF_TOKEN` before starting; choose a private local password.

   ```bash
   cp .env.example .env
   sed -i 's|^HUGGINGFACE_TOKEN=.*|HUGGINGFACE_TOKEN=YOUR_HF_TOKEN|' .env
   sed -i 's|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=meeting-local-password|' .env
   sed -i 's|^DATABASE_URL=.*|DATABASE_URL=postgresql://meeting:meeting-local-password@127.0.0.1:55432/meeting|' .env
   ```

2. Start Ollama locally and download the chat and embedding models.

   ```bash
   docker volume create meeting-ai-ollama
   docker run -d --name meeting-ai-ollama --restart unless-stopped \
     --network host -v meeting-ai-ollama:/root/.ollama ollama/ollama
   docker exec meeting-ai-ollama ollama pull qwen3
   docker exec meeting-ai-ollama ollama pull nomic-embed-text
   curl -s http://127.0.0.1:11434/api/tags
   ```

   Copy the `digest` for `nomic-embed-text:latest` from the last command, then replace the existing RAG lines in `.env` with the following, replacing `EMBEDDING_DIGEST` with that value:

   ```dotenv
   RAG_ENABLED=true
   RAG_PROVIDERS_JSON='[{"id":"local","name":"Ollama","kind":"ollama","base_url":"http://127.0.0.1:11434","chat_models":[{"id":"qwen3","context_tokens":8192,"output_tokens":1024}],"embedding_models":["nomic-embed-text:latest"]}]'
   RAG_EMBEDDING_PROFILES_JSON='[{"id":"local-embed-v1","provider_id":"local","model":"nomic-embed-text:latest","revision":"EMBEDDING_DIGEST","dimensions":768,"chunk_revision":"utf8-v1"}]'
   ```

3. Start Meeting AI with the RAG index worker as a managed service.

   ```bash
   docker compose --profile rag up -d --build
   docker compose ps
   ```

   Use `--profile rag` when rebuilding or recreating services so the index worker
   also starts. It uses the shared migrations and `.env`; enable `RAG_ENABLED`
   before starting it. Do not also run a manual RAG worker. Without RAG, omit
   `--profile rag`. Failed index jobs require a separate backfill; rebuilding
   does not reset them.

4. Bootstrap the first Admin after the migration service succeeds. The command prompts for email and password; keep the password out of shell arguments.

   ```bash
   docker compose exec api uv run --no-sync python -m app.commands.bootstrap_admin
   ```

5. Open `http://127.0.0.1:5173/app/`, sign in, create a project and assign members. The first transcription downloads the speech models and can take several minutes. View logs with `docker compose logs --tail=100 api worker migrate`.

`docker compose down` preserves data and models. Do not run `docker compose down -v` unless you intend to delete them.

### Native development

Install Python 3.10+, uv and FFmpeg. Configure PostgreSQL with pgvector and use the same `DATABASE_URL` and private data root for the API and workers. The Compose setup uses host networking and targets Linux.

```bash
uv sync
PYTHONPATH=apps/api uv run --env-file .env python -m app.db.migrate
PYTHONPATH=apps/api uv run --env-file .env uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Start the meeting worker in another terminal:

```bash
PYTHONPATH=apps/api uv run --env-file .env python -m app.services.meetings.worker
```

For web development, install Node.js and npm, then run from `apps/web`:

```bash
npm ci
npm run dev
```

## 6. Configuration

Copy `.env.example` to `.env` and keep credentials out of Git. Shell variables override values loaded with `uv run --env-file .env`.

| Variables | Purpose |
| --- | --- |
| `HUGGINGFACE_TOKEN` | Access to gated pyannote models after accepting the model agreement |
| `PHOWHISPER_MODEL`, `WHISPER_MODEL`, `SILERO_LANG95_MODEL`, `PYANNOTE_MODEL` | Speech and diarization model sources |
| `MODEL_DEVICE` | `auto`, `cpu` or `cuda` |
| `DATABASE_URL`, `POSTGRES_*` | PostgreSQL connection and Compose settings |
| `MEETING_DATA_DIR` | Private audio root shared by application processes |
| `BOT_CHROME_PATH`, `BOT_AUDIO_SINK`, `BOT_ADMISSION_TIMEOUT_SECONDS` | Meet bot browser, dedicated audio sink and admission timeout |
| `RAG_ENABLED`, `RAG_PROVIDERS_JSON`, `RAG_EMBEDDING_PROFILES_JSON` | Opt-in RAG and provider/embedding allowlists |
| `RAG_MAX_K`, `RAG_MIN_COSINE_SIMILARITY`, `RAG_PROMPT_VERSION` | Retrieval limits, initial uncalibrated threshold and prompt version |
| `DEEPSEEK_API_KEY` | Optional server-side DeepSeek credential |
| `PYANNOTE_REVISION`, `VOICE_IDENTIFICATION_ENABLED` | Voice enrollment revision and automatic matching switch |
| `AUTH_COOKIE_SECURE` | Require HTTPS and Secure session cookies |
| `TEST_DATABASE_URL` | Dedicated disposable database for integration tests |

### DeepSeek API (optional)

DeepSeek chat uses the existing `openai_compatible` adapter; no extra SDK is needed.
Set `DEEPSEEK_API_KEY` in `.env` and append the following object to the existing
`RAG_PROVIDERS_JSON` array (keep the Ollama object and embedding profiles):

```json
{"id":"deepseek","name":"DeepSeek","kind":"openai_compatible","base_url":"https://api.deepseek.com","api_key_env":"DEEPSEEK_API_KEY","external_data_enabled":true,"chat_models":[{"id":"deepseek-flash","context_tokens":1000000,"output_tokens":100000}]}
```

`RAG_PROVIDERS_JSON` must remain an array: `RAG_PROVIDERS_JSON='[{...existing Ollama provider...},{...DeepSeek provider above...}]'`
(replace the placeholders with the actual objects). Do not replace the entire
array with the DeepSeek object: embedding profiles still require their provider.

`external_data_enabled=true` explicitly permits sending questions and retrieved
meeting text, or transcripts for summaries, to DeepSeek. Obtain approval before
using production meeting data. The key stays on the server and is not returned
by `/api/chat/providers`. Embeddings continue to use your existing local provider.
The token values above are application budgets, not the model's advertised limits.
See the [DeepSeek API documentation](https://api-docs.deepseek.com/) for model availability.

Recreate the API and worker to load the updated environment:

```bash
docker compose --profile rag up -d --force-recreate api worker rag-worker
```

Select **DeepSeek / deepseek-flash** in the chat provider and model selectors.
API clients use `chat_provider_id="deepseek"` and `chat_model_id="deepseek-flash"`.
Automatic summaries use the first configured provider with chat models; put
DeepSeek first only if you also want new summaries to use it. Existing saved
summaries are retained. To disable DeepSeek, remove its object
from `RAG_PROVIDERS_JSON` and recreate the services again.

### Workspace system prompts

Admins can edit separate summary and RAG system prompts in **Settings → System prompt**.
Both fields apply across the workspace and are stored in PostgreSQL. Leave a field
empty to use its built-in prompt, or choose **Khôi phục mặc định** and save. Each
field allows 8,000 characters. Fixed JSON output, evidence and citation rules remain
in effect; transcript text stays in the user message. Prompts consume model context.
Updates apply to new calls without restarting services. Existing saved summaries
remain unchanged. New custom summaries record the effective prompt hash in their
prompt version; RAG evaluation state reports the effective prompt hash.

Apply migration 007 explicitly before running the new API. Compose applies it via
the migration service. It adds only `workspace_system_prompts`; no existing meeting
data is changed. The admin API is `GET`/`PUT /api/admin/system-prompts`, with JSON
fields `summary_system_prompt` and `rag_system_prompt`. Both routes require an
Admin session; PUT also requires CSRF. The feature works with the existing provider
configuration and does not enable external data sharing.

For rollback, first clear both fields and save to restore built-in behavior.
Keep the database backup and use the compatible application release; schema checks
require matching migration history. Do not delete migration rows or drop meeting
data to downgrade. Prompt quality requires a separate held-out evaluation; tests
only verify persistence, access, prompt propagation and output validation.

### Project access migration

Migrations 005–006 add project access, voice enrollment ownership and sessions. They keep existing meetings and bot sessions without a project for manual cleanup; new records require a project. Back up the database and private data directory, stop the API and workers, run `PYTHONPATH=apps/api uv run --env-file .env python -m app.db.migrate`, then start the updated services. The migration runner applies numbered migrations in order; Compose runs its migration service before API/worker startup. HTTP requests never apply migrations.

For rollback, stop all updated services and retain the current database and private audio backup, including records created after the upgrade. Restore the reviewed pre-upgrade database backup into a separate database and select the matching application version; verify it before switching `DATABASE_URL`. Reconcile post-upgrade recordings manually before reopening writes. There is no automatic down migration: do not drop access tables, project columns, recordings or historical rows to downgrade.

Stored meeting routes require a signed-in session; uploads, deletion and retries also require the `X-CSRF-Token` header. Upload multipart forms must select `project_id`. Lists and transcript search accept an optional `project_id` filter and return only accessible projects. Archived projects remain readable but reject uploads. Members may delete or retry only their own uploads; project managers and Admin may act on any meeting in their scope. Idempotency keys replay only for the same account, project and audio. Meeting metadata includes `project_id` and `uploaded_by_account_id`. Historical projectless meetings remain hidden, including from Admin.

Bot session creation requires `project_id` and an active project membership with recording rights. Bot reads require project access; writes require CSRF. Members control their own unfinalized sessions; managers and Admin control sessions in their scope. Viewers can read status. Active and interrupted-session discovery hides inaccessible and historical projectless sessions. One bot remains globally active at a time. Bot idempotency keys bind the account, project and Meet link. Finalized recordings retain their project and bot source; members cannot delete the resulting meeting.

## 7. Usage & API Documentation

Open `http://127.0.0.1:5173/app/`, sign in and open an assigned project to upload audio or use a configured local Meet bot. Review completed transcripts and summaries. Meeting chat requires enabled RAG, a configured embedding profile and indexed meetings.

Interactive API documentation: `http://127.0.0.1:8000/docs`. OpenAPI schema: `http://127.0.0.1:8000/openapi.json`.

### Stateless audio processing

```bash
curl --fail-with-body -X POST http://127.0.0.1:8000/api/audio/process \
  -F 'file=@/path/to/meeting.wav'
```

The current route accepts FFmpeg-supported audio up to 100 MiB and 30 minutes. FFmpeg decodes to 16 kHz mono float32 samples. The response follows `AudioProcessResponse` in `apps/api/app/schemas/transcript.py`; timestamps refer to original audio and overlapping diarization turns are retained. Invalid audio returns HTTP 400, oversized uploads return 413, and unavailable models return 503. This local endpoint is unauthenticated and does not persist results.

### Local accounts

Apply shared migrations before starting the API. Create the first administrator with
`PYTHONPATH=apps/api uv run --env-file .env python -m app.commands.bootstrap_admin`;
it prompts for a password without echo and refuses a second administrator bootstrap.
Registration (`POST /api/auth/register`) creates an unassigned account; email identifiers
are unique but mailbox ownership is not verified. Passwords require 12–1024 characters.

Login and registration return an account and CSRF token, and set an HttpOnly,
SameSite=Strict cookie scoped to `/api`. Read `GET /api/auth/session` to restore the
session and CSRF token. Send `X-CSRF-Token` on authenticated writes, including logout
and password change. Password change requires `current_password` and `new_password`
and revokes every account session. Sessions expire after one idle hour or seven days;
disabled accounts cannot authenticate. Login and registration share a PostgreSQL
limit of 20 attempts per peer per 15 minutes. Authentication remains loopback-only.
Set `AUTH_COOKIE_SECURE=true` for HTTPS; this rejects HTTP authentication requests.
Public company-network deployment still requires a separate boundary review.

The web client requires a local account session. Sign in at `/login` or register at `/signup`, then open a project assigned by an administrator or project manager under `/app/`. Both authentication pages offer light/dark mode and remember the choice in this browser. Opening a protected page while signed out redirects to login and returns to that page after authentication. Settings contains logout and, for administrators, account management at `/app/settings/accounts`; the old `/app/accounts` link redirects there. Administrators create and archive projects and manage accounts; managers manage project members. Audio uploads from a project include its project ID. Session cookies remain HttpOnly, and the client attaches the session CSRF token to writes.

Settings also lists the signed-in account's accessible projects, its role in each project, and archived status, with links to open each project. The UI displays `system_admin` as **Admin** and `project_manager` as **Project Manager**; API role values are unchanged.

| Action | Admin | Project manager | Member | Viewer |
| --- | --- | --- | --- | --- |
| Read meetings/audio/transcripts, summaries and RAG | All projects | Assigned projects | Assigned projects | Assigned projects |
| Upload or start bot in active project | Yes | Yes | Yes | No |
| Delete/retry upload | Any | Any in project | Own only | No |
| Delete resulting bot meeting | Yes | Yes in project | No | No |
| Manage members/viewers | Yes | Yes in project | No | No |
| Appoint managers; create/rename/archive/restore projects | Yes | No | No | No |
| Create/update shared voice profiles | Yes | Yes | No | No |
| Delete shared voice profiles or samples | Yes | No | No | No |

Shared voice names are company-wide; meeting-derived samples and enrollment jobs still require access to their source project. Members can control their own unfinalized bot sessions. Revoking membership takes effect on subsequent resource checks. Archived projects remain readable and searchable and permit membership maintenance, while refusing new uploads and recordings. Chat defaults to all accessible projects; client meeting IDs only narrow that scope. Unassigned accounts see no meetings. Confirm a person's identity outside the app before granting access: registration does not verify email ownership.

`/api/audio/process` remains a stateless, unauthenticated local API with its existing response contract. It does not read stored meetings. Keep API ports bound to loopback. Before company-network hosting, configure HTTPS, Secure cookies and trusted deployment origins, review every exposed endpoint (including stateless processing), and review proxy/Host/Origin handling. The current local guards intentionally reject remote peers; enabling Secure cookies alone does not enable network hosting.

For password recovery, a trusted administrator with local database access must first
confirm the person's identity outside the app; email ownership is unverified.
Run `PYTHONPATH=apps/api uv run --env-file .env python -m app.commands.recover_password`
on the local host (or through the API container, as with bootstrap). It prompts for
the account email and a new password twice without echo. Never put passwords in
arguments, environment variables or logs. The command changes only the existing
account's password and revokes all its sessions in one transaction; it does not
change roles or re-enable disabled accounts. Restrict host/database access to trusted
operators and communicate the temporary credential privately; the user should then
change it through their account. There is no public password reset endpoint.

## 8. Testing & Evaluation

Run API tests from the repository root. Tests use fake inference models and real FFmpeg decoding. PostgreSQL integration tests require an isolated `TEST_DATABASE_URL` and use random schemas.

```bash
PYTHONPATH=apps/api uv run python -m unittest discover -s apps/api/tests -p 'test_*.py'
```

Run web checks from `apps/web`:

```bash
npm run typecheck
npm test
npm run build
```

Run fast offline evaluation unit tests from the repository root:

```bash
uv run python -m unittest discover -s ai_training/evaluation -p '*_unit.py'
```

Real-model evaluations require datasets, model downloads and any required Hugging Face access:

```bash
uv run python ai_training/evaluation/test_vivos_silero_phowhisper.py --dataset-root <vivos-directory> --limit 5
uv run python ai_training/evaluation/evaluate_vietsuperspeech_silero_phowhisper.py --dataset-root <dataset-directory> --limit 5
uv run --env-file .env python ai_training/evaluation/test_viyt_vad_pyannote.py --dataset-root <dataset-directory> --limit 5
```

Limited runs are smoke checks, not quality benchmarks. Report dataset version/split, sample count, model IDs/revisions, preprocessing, device, dependency versions and timing with WER/CER for ASR or VAD/DER/JER for diarization. Model, prompt and threshold changes require held-out baseline/candidate comparisons and known regressions. Keep generated reports under ignored `ai_training/results/`; do not commit private recordings, transcripts, embeddings or model weights.

No Python formatter, linter or type checker is currently configured. These commands document verification workflows and do not assert that evaluations have passed.
