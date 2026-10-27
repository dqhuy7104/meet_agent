# Meeting AI

## Components

- **FastAPI + worker:** upload, transcription, diarization, meeting processing, and RAG.
- **React:** local UI at `http://127.0.0.1:5173/app/`.
- **PostgreSQL + pgvector:** meeting metadata, transcripts, jobs, and RAG index.
- **Ollama:** local models for summaries and RAG search.

Audio is kept on the local Docker volume. The API only listens on loopback; do not expose it publicly.

## Requirements

- Docker Engine with Docker Compose
- A Hugging Face token accepted for `pyannote/speaker-diarization-community-1`
- At least 12 GB free disk space for images and local models

## Quick start

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

3. Start Meeting AI, then start the local RAG index worker in a second terminal.

   ```bash
   docker compose up -d --build
   docker compose ps
   ```

   ```bash
   docker compose exec worker uv run --no-sync python -m app.services.rag.worker --migrate --backfill
   ```

4. Bootstrap the first Admin after the migration service succeeds. The command prompts for email and password; keep the password out of shell arguments.

   ```bash
   docker compose exec api uv run --no-sync python -m app.commands.bootstrap_admin
   ```

5. Open `http://127.0.0.1:5173/app/`, sign in, create a project and assign members. The first transcription downloads the speech models and can take several minutes. View logs with `docker compose logs --tail=100 api worker migrate`.

`docker compose down` preserves data and models. Do not run `docker compose down -v` unless you intend to delete them.

## Project access migration

Migrations 004–005 add project access, voice enrollment ownership and sessions. They keep existing meetings and bot sessions without a project for manual cleanup; new records require a project. Back up the database and private data directory, stop the API and workers, run `PYTHONPATH=apps/api uv run --env-file .env python -m app.db.migrate`, then start the updated services. The migration runner applies numbered migrations in order; Compose runs its migration service before API/worker startup. HTTP requests never apply migrations.

For rollback, stop all updated services and retain the current database and private audio backup, including records created after the upgrade. Restore the reviewed pre-upgrade database backup into a separate database and select the matching application version; verify it before switching `DATABASE_URL`. Reconcile post-upgrade recordings manually before reopening writes. There is no automatic down migration: do not drop access tables, project columns, recordings or historical rows to downgrade.

Stored meeting routes require a signed-in session; uploads, deletion and retries also require the `X-CSRF-Token` header. Upload multipart forms must select `project_id`. Lists and transcript search accept an optional `project_id` filter and return only accessible projects. Archived projects remain readable but reject uploads. Members may delete or retry only their own uploads; project managers and Admin may act on any meeting in their scope. Idempotency keys replay only for the same account, project and audio. Meeting metadata includes `project_id` and `uploaded_by_account_id`. Historical projectless meetings remain hidden, including from Admin.

Bot session creation requires `project_id` and an active project membership with recording rights. Bot reads require project access; writes require CSRF. Members control their own unfinalized sessions; managers and Admin control sessions in their scope. Viewers can read status. Active and interrupted-session discovery hides inaccessible and historical projectless sessions. One bot remains globally active at a time. Bot idempotency keys bind the account, project and Meet link. Finalized recordings retain their project and bot source; members cannot delete the resulting meeting.

## Local accounts

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

The web client requires a local account session. Sign in or register, then open a project assigned by an administrator or project manager. Administrators create and archive projects and manage accounts; managers manage project members. Audio uploads from a project include its project ID. Session cookies remain HttpOnly, and the client attaches the session CSRF token to writes.

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
