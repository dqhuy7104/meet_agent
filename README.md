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

4. Open `http://127.0.0.1:5173/app/`. The first transcription downloads the speech models and can take several minutes. View logs with `docker compose logs --tail=100 api worker migrate`.

`docker compose down` preserves data and models. Do not run `docker compose down -v` unless you intend to delete them.

## Project access migration

Migration 004 adds accounts, projects, and memberships. It keeps existing meetings and bot sessions without a project for manual cleanup; new records require a project. Run the shared migration explicitly before starting updated services. Roll back by restoring a reviewed pre-migration database backup or writing a reviewed reverse migration for the affected deployment. Do not silently drop the new tables or historical data.

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
