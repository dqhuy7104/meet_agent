# 1. Project Overview

- **Purpose:** Meeting AI extracts speech and speaker information from meeting audio. The intended next stage is a retrieval-augmented generation (RAG) system for searching and answering questions about meeting content.
- **Primary users:** Developers integrating the API now; people reviewing and querying meetings when the web and RAG flows are implemented.
- **Implemented flow:** Audio upload → FastAPI `/api/audio/process` → FFmpeg decode to 16 kHz mono → Silero VAD → PhoWhisper ASR and pyannote diarization → transcript and timestamped speaker segments in the HTTP response. Models load on first use and are reused within an API process. No transcript is persisted by this flow.
- **Planned flow:** Meeting ingestion → transcript cleanup and summarization → embedding and indexing → hybrid retrieval and reranking → grounded chat. `pipeline.md` sketches this direction; its storage, agent, and client stages are not implemented.
- **Tech stack:** Python 3.10+, uv, FastAPI/Pydantic, FFmpeg, PyTorch, Silero VAD, Transformers/PhoWhisper, and pyannote.audio. `apps/web` is a TypeScript scaffold. There is no working database, queue, worker, or deployment stack yet.
- **Definition of done:** Scope changes to the implemented or explicitly requested flow; verify affected behavior, document contract or model changes, protect meeting data and secrets, and preserve existing API behavior unless a migration is agreed.

# 2. Repository Map

```text
.
├── apps/
│   ├── api/
│   │   ├── app/api/routes/   # Audio route; other route files are placeholders
│   │   ├── app/core/         # Model settings and lazy model registry
│   │   ├── app/pipelines/    # Implemented audio processing orchestration
│   │   ├── app/schemas/      # Audio response models; other schemas are placeholders
│   │   ├── app/services/     # Audio decoder; planned indexing, retrieval, LLM services
│   │   └── tests/            # Isolated audio API and model-registry tests
│   └── web/                  # TypeScript client scaffold; no runnable app scripts
├── ai_training/
│   ├── datasets/            # Dataset notes and download scripts
│   ├── evaluation/          # VIVOS, VietSuperSpeech, and ViYT-Diar evaluations
│   ├── training/            # ASR and diarization training scaffolds
│   ├── notebooks/           # Experiments
│   └── results/             # Generated evaluation output; generally ignored
├── shared/python/           # Shared Python scaffolds, not active contracts yet
├── infra/                   # Infrastructure scaffolds
├── scripts/                 # Development/training scaffolds and seed stub
├── pipeline.md              # Proposed end-to-end meeting/RAG pipeline
├── pyproject.toml           # Root Python dependencies; uv.lock pins them
├── .env.example             # Sanitized audio model settings
└── AGENTS.md
```

# 3. How Agents Should Work

1. Read `README.md`, `pyproject.toml` or `apps/web/package.json` as relevant, and any nearer `AGENTS.md` before editing.
2. Identify the owner: API runtime in `apps/api`, model and dataset experiments in `ai_training`, and future client work in `apps/web`. Inspect the entry point, response schema, and existing tests before changing behavior.
3. For nontrivial work, state a short plan and affected directories. Distinguish working code from the placeholders in `pipeline.md` and the scaffold files.
4. Keep edits scoped; do not build unrelated planned stages or change dependencies without a concrete need.
5. Update focused tests and relevant documentation when behavior, API responses, model settings, evaluation methods, or deployment requirements change.
6. Report what changed, commands actually run, and any remaining risks or checks not run. Never claim a benchmark or model evaluation passed unless it ran.

# 4. Workspace Boundaries

- `apps/api/app/api/routes` handles HTTP upload limits, validation, and error mapping; `schemas` defines response contracts; `pipelines` coordinates inference; `services` owns decoding and future retrieval/LLM logic; `core` owns settings and lazy model loading.
- `apps/web` owns presentation and API consumption when implemented. Keep model execution and provider secrets on the server.
- `ai_training` owns dataset acquisition, experiments, and offline evaluation. Its scripts must not become request-time API dependencies by accident.
- `shared/python` may hold genuinely shared code once there are concrete consumers; it is currently scaffold only. Keep dependencies from shared code toward apps out of the graph.
- `infra` and root Docker/Compose files are placeholders. Add deployable configuration only with its runtime requirements documented; never embed credentials.

The current API has no worker or persistence layer. If adding either for RAG, define ownership, contracts, retries, and idempotency explicitly rather than assuming they already exist.

# 5. Coding Conventions

- Use uv for Python commands and the root `pyproject.toml`/`uv.lock` for Python dependencies. Follow the existing typed Python and Pydantic style; do not invent web scripts or tooling.
- Keep routes, schemas, orchestration, and model services separate. Prefer small domain-specific functions over general frameworks for stages that are still placeholders.
- Validate uploaded bytes, size, duration, and future query inputs at their boundaries. Return explicit response models and actionable HTTP errors.
- Keep timestamps in seconds relative to the original uploaded audio, and preserve overlap in diarization output when changing transcript alignment.
- Preserve `/api/audio/process` and `AudioProcessResponse` fields unless a contract change is requested. Add migrations and rollback notes if persistence is introduced.

# 6. AI/ML-Specific Rules

- **Audio contract:** FFmpeg converts uploads to 16 kHz mono float32 samples. The route caps uploads at 25 MiB; the decoder caps duration at 30 minutes. VAD regions, speaker turns, and transcript segments use original-audio seconds. Each transcript region receives the speaker with greatest overlap; all diarization turns remain in the response.
- **Model lifecycle:** Keep Silero VAD, PhoWhisper, and pyannote independently loadable and cached per API process. Respect `MODEL_DEVICE`; preserve CPU fallback for `auto`, and report model load failures without exposing tokens or audio.
- **Reproducibility:** Record model IDs/revisions, preprocessing, device, relevant dependency versions, dataset split, and seeds where applicable for training or evaluation changes.
- **Datasets and metrics:** Keep VIVOS, VietSuperSpeech, and ViYT-Diar source, license, splits, and transformations traceable. Use held-out examples and compare WER/CER for ASR and VAD/DER/JER for diarization, plus latency when model behavior changes. Do not treat a smoke test as a quality result.
- **Future RAG:** Define chunking, embedding model/version, index schema, retrieval/reranking rules, and citations before exposing meeting answers. Treat transcript and retrieved text as untrusted; validate LLM output and handle timeout and empty-result paths. Keep prompts versioned when introduced.
- **Privacy:** Meeting audio and transcripts may contain personal data. Avoid raw-content logs, protect secrets, define retention before persistence, and require approval before sending production data to third-party model providers.
- **Change gate:** Model, prompt, preprocessing, feature extraction, or threshold changes need an evaluation note with dataset version, baseline and candidate metrics, latency/cost, and known regressions.

# 7. Configuration and Secrets

- The audio API reads `PHOWHISPER_MODEL`, `PYANNOTE_MODEL`, `MODEL_DEVICE` (`auto`, `cpu`, or `cuda`), and `HUGGINGFACE_TOKEN` through `apps/api/app/core/config.py`. Remote pyannote loading needs the token and accepted model agreement.
- Keep `.env.example` sanitized and load local values with `uv run --env-file .env` as shown in `README.md`; shell variables override values from that file.
- Never commit tokens, private recordings/transcripts, raw datasets, model weights, embeddings, or generated caches. `ai_training/results/` is for generated reports, not source-controlled metrics.
- Document any new RAG provider, storage, or deployment variables before use. Do not change a production model/provider default without explicit approval.

# 8. Commands

| Task | Command |
| --- | --- |
| Install Python dependencies | `uv sync` |
| Start audio API | `PYTHONPATH=apps/api uv run --env-file .env uvicorn app.main:app --host 0.0.0.0 --port 8000` |
| API tests (fake models, real FFmpeg decoding) | `PYTHONPATH=apps/api uv run python -m unittest discover -s apps/api/tests -p 'test_*.py'` |
| Offline evaluation unit tests | `uv run python -m unittest discover -s ai_training/evaluation -p '*_unit.py'` |
| VIVOS ASR evaluation | `uv run python ai_training/evaluation/test_vivos_silero_phowhisper.py --dataset-root <vivos-directory> --limit 5` |
| VietSuperSpeech ASR evaluation | `uv run ai_training/evaluation/evaluate_vietsuperspeech_silero_phowhisper.py --dataset-root <dataset-directory> --limit 5` |
| ViYT-Diar VAD/diarization evaluation | `uv run --env-file .env python ai_training/evaluation/test_viyt_vad_pyannote.py --dataset-root <dataset-directory> --limit 5` |
| Format, lint, type check, web build, service build | No configured commands yet; `Makefile`, `apps/web/package.json`, Dockerfiles, and Compose are placeholders. |

Install FFmpeg separately for API audio processing. Real-model evaluations need datasets, model downloads, and (for remote pyannote) a Hugging Face token. Inspect manifests and scripts again before adding or changing commands; run the smallest relevant checks first.

# 9. Testing and Acceptance

- **Current API:** Test upload limits, empty/invalid/too-long audio, no-speech behavior, model-load errors, transcript text, speaker assignment, and overlapping speaker turns as behavior changes. Existing API tests use fake inference models and FFmpeg for decoding.
- **Model registry:** Keep lazy initialization, one load per model per process, and retry after failed loading covered by isolated tests.
- **Offline evaluation:** Keep dataset parsing and metric calculations in fast unit tests. Run real-model VIVOS/VietSuperSpeech and ViYT-Diar evaluations separately, with a fixed split and reported sample count, model ID, WER/CER or VAD/DER/JER, and timing.
- **Future RAG:** When indexing and chat become operational, add tests for ingestion through retrieval and cited answers, empty or irrelevant retrieval, permission boundaries, and retry/failure behavior. Do not claim these flows are currently covered.
- **Nonfunctional:** Check inference latency and memory/VRAM for model changes; check accessibility when a web UI is implemented. Keep fast tests independent of model downloads and external providers.

# 10. Documentation and Change Management

- Update `README.md` for setup, audio API behavior, or new user-facing flows. Keep `.env.example` aligned with runtime settings.
- Treat `pipeline.md` as a proposed design: update it when the intended audio-to-RAG architecture changes, and mark newly implemented stages clearly. Record major cross-directory decisions in a design note.
- Update Pydantic response models, route documentation, and examples together when the HTTP contract changes.
- Describe breaking changes, migrations, rollback, and model-evaluation results in the change description when applicable.

# 11. Prohibited Shortcuts

- Do not bypass upload validation, future authorization checks, or model error handling to make tests pass.
- Do not silently alter datasets, labels, ground truth, evaluation thresholds, or text normalization used for reported metrics.
- Do not fabricate metrics, test results, meeting citations, or benchmark performance. Do not present `pipeline.md` placeholders as working features.
- Do not commit generated evaluation output, embeddings, large model files, recordings, or private transcripts unless an approved mechanism explicitly requires them.
- Do not make fast unit tests depend on external model downloads, tokens, or network access.

# 12. Scoped Agent Instructions

There are currently no nearer `AGENTS.md` files. If a directory needs extra rules, add them at the owning location:

- `apps/api/AGENTS.md` for HTTP contracts, audio runtime, and future RAG services.
- `ai_training/AGENTS.md` for dataset licensing, evaluation, and model reproducibility.
- `apps/web/AGENTS.md` for UI, accessibility, and client tests once the app is implemented.
- `infra/AGENTS.md` for deployment safeguards once infrastructure is added.

More-specific instructions should supplement the root rules rather than contradict them.


# 13. CODE STANDARDS AND GUIDELINES

1. Simpler is better - do not overcomplicate
2. Comments only when necessary
3. Be concise, short README, no emojis
4. IMPORTANT: Avoid overly defensive programming; avoid isinstance checks, only manage exceptions when necessary
5. Use uv; ALWAYS 'uv run xxx' NEVER 'python3 xxx'
6. Prefer clear, domain-specific names over abbreviations; name booleans as questions and collections as plurals.
7. Keep functions focused on one responsibility. Extract a helper when it makes the caller easier to read or test.
8. Keep modules cohesive and preserve layer boundaries: routes handle HTTP, services contain business logic, and schemas define API contracts.
9. Add type hints to public functions and keep request/response models explicit.
10. Validate untrusted input at system boundaries and return actionable, consistent error messages.
11. Avoid duplication, but do not introduce abstractions until at least two concrete uses justify them.
12. Prefer early returns to deeply nested conditionals. Keep the happy path obvious.
13. Keep configuration in settings or environment variables; do not hard-code secrets, paths, model IDs, or deployment-specific values.
14. Add or update focused tests for changed behavior, including failure cases when they are part of the contract.
15. Run the relevant formatter, linter, type checks, and tests before considering a change complete.
16. Keep commits and pull requests narrowly scoped; explain non-obvious design decisions in the change description.

# 14. SUMMARIZE PROJECT
