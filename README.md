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
