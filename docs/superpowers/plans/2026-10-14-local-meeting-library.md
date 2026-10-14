# Local Meeting Library API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a local meeting library API that stores uploaded audio, processes it in a separate worker, and supports listing, detail, search, audio playback, retry, and deletion.

**Architecture:** SQLite stores metadata, job state, timestamped results, and an FTS5 index; original audio lives under a configured local directory. A single worker process owns the existing `process_audio` model registry and claims queued jobs transactionally. New routes allow loopback peers only; the existing `/api/audio/process` route is untouched.

**Tech Stack:** Python 3.10+, FastAPI/Pydantic, standard-library `sqlite3` and `fcntl`, SQLite FTS5, FFmpeg, uv, unittest/httpx.

**Spec:** `docs/superpowers/specs/2026-10-14-local-meeting-library-design.md`

## Global Constraints

- Keep `POST /api/audio/process` and `AudioProcessResponse` behavior and fields unchanged.
- Upload limit: 25 MiB; decoded duration limit: 30 minutes; timestamps are original-audio seconds.
- One local worker; interruption recovery retries a job only if it has been claimed fewer than two times.
- No account system, web client, cloud service, summarization, embedding, or chat.
- New routes allow loopback clients only; documented launch binds `127.0.0.1` and forbids proxy exposure.
- Retain meeting data until deletion; never log raw audio, transcript text, tokens, or internal paths.
- Use the root `pyproject.toml` and `uv.lock`; no new dependency is needed.

## File Map

- `apps/api/app/core/config.py`: add and validate `MEETING_DATA_DIR` while preserving model defaults.
- `apps/api/app/db/session.py`: create SQLite connections, schema, FTS5 table, and transactions.
- `apps/api/app/services/meetings/storage.py`: private audio directory, atomic file publication, cleanup, and generated-key resolution.
- `apps/api/app/services/meetings/repository.py`: metadata, job transitions, result persistence, deletion, and search SQL.
- `apps/api/app/services/meetings/worker.py`: exclusive worker lock, recovery, one-job processing, and safe failure mapping.
- `apps/api/app/schemas/meeting.py`: explicit metadata, detail, search, and upload response models.
- `apps/api/app/api/routes/meetings.py`: loopback gate, validation, HTTP mapping, and all new endpoints.
- `apps/api/app/main.py`: mount the meeting router without altering the audio router.
- `apps/api/app/services/audio/preprocess.py`: distinguish overlong audio via an `InvalidAudioError` subclass while preserving its existing message.
- `apps/api/tests/test_meeting_store.py`, `test_meeting_routes.py`, `test_meeting_worker.py`, `test_meeting_search.py`, `test_meeting_flow.py`: focused tests.
- `.env.example`, `.gitignore`, `README.md`: data path, private local launch and worker commands, retention, backup, and rollback.

## Review Focus

- A forged `Host` or `X-Forwarded-For` header from a non-loopback peer must not bypass the route gate (Task 2).
- An upload crash after file publication but before database insertion must leave only an unreferenced file that startup cleanup removes (Task 1).
- A repeated idempotency key with identical bytes but a different title must return the original record without changing it (Task 2).
- A worker exception after claiming a job must commit `failed` with a safe code and no partial result rows (Task 3).
- A range request beyond the audio length must return HTTP 416 without reading another file (Task 4).

---

### Task 1: Private Storage and SQLite Schema

**Files:** Modify `apps/api/app/core/config.py`, `.env.example`, `.gitignore`; replace `apps/api/app/db/session.py`; create `apps/api/app/services/meetings/storage.py`, `apps/api/app/services/meetings/repository.py`, `apps/api/tests/test_meeting_store.py`.

**Interfaces:** Produce `Settings.meeting_data_dir: Path`; `open_meeting_db(data_dir: Path) -> sqlite3.Connection`; `initialize_meeting_store(data_dir: Path) -> None`; `publish_audio(data_dir: Path, data: bytes) -> str`; `audio_path(data_dir: Path, storage_key: str) -> Path`; `cleanup_orphaned_audio(data_dir: Path) -> None`; `MeetingRepository(connection: sqlite3.Connection)` with `create_meeting(meeting_id: str, title: str | None, filename: str, storage_key: str, checksum: str, idempotency_key: str | None) -> MeetingRecord`, `get_meeting(meeting_id: str) -> MeetingRecord | None`, and `list_meetings(limit: int, offset: int) -> list[MeetingRecord]`. Define `MeetingRecord` as a typed dataclass in `repository.py`; include the spec's metadata fields. Later tasks extend this repository.

- [ ] **Step 1: Write failing tests** in `test_meeting_store.py`: schema creation is idempotent; directory and files are owner-only; published names are generated and unrelated to filenames; failed insert can be followed by file removal; startup cleanup removes an unreferenced published file and abandoned temporary file but retains a referenced one; a duplicate idempotency key raises a distinct repository conflict.
- [ ] **Step 2: Run** `PYTHONPATH=apps/api uv run python -m unittest apps.api.tests.test_meeting_store -v`; expect failure because the store interfaces do not exist.
- [ ] **Step 3: Implement** the interfaces above. Use `MEETING_DATA_DIR` with default `./meeting-data`, resolved at startup; reject an empty value. Create data, audio, and temporary directories with mode `0700`, audio with mode `0600`; use UUID-derived storage keys and `os.replace` after flushing/fsyncing a temporary file. SQLite uses foreign keys, WAL, and FTS5; tables are `meetings`, `vad_segments`, `diarization_segments`, `transcript_segments`, and `transcript_fts`. The unique nullable `idempotency_key` and checksum live in `meetings`. Cleanup compares generated keys against database references and never follows client-supplied paths. Ignore `meeting-data/` in Git.
- [ ] **Step 4: Run** the same test command; expect all Task 1 tests to pass.
- [ ] **Step 5: Commit** the Task 1 files with `feat: add private meeting store`.

### Task 2: Local Upload and Idempotency

**Files:** Modify `apps/api/app/api/routes/meetings.py`, `apps/api/app/schemas/meeting.py`, `apps/api/app/main.py`, `apps/api/app/services/meetings/repository.py`; create `apps/api/tests/test_meeting_routes.py`.

**Interfaces:** Consume Task 1 store functions and `MeetingRecord`. Produce `require_loopback(request: Request) -> None`, `POST /api/meetings` with multipart `file` and optional `title`, and `MeetingAcceptedResponse(meeting_id: str, status: Literal['queued'])`. Repository adds `find_by_idempotency_key(key: str) -> MeetingRecord | None`. Use a dependency that opens the configured database per request and closes it afterward.

- [ ] **Step 1: Write failing route tests** for 202 queued response; empty bytes 400; 25 MiB + 1 byte 413; repeated key/same checksum returns original ID and original title; repeated key/different checksum 409; no key creates a new ID; failed insert deletes its published file; non-loopback peer 403 even with loopback-looking proxy or Host headers. Include 25 MiB exactly as accepted without decoding in the request path.
- [ ] **Step 2: Run** `PYTHONPATH=apps/api uv run python -m unittest apps.api.tests.test_meeting_routes -v`; expect endpoint/import failures.
- [ ] **Step 3: Implement** `require_loopback` using `ipaddress.ip_address(request.client.host).is_loopback`, ignoring proxy headers; validate title length at 200 characters and idempotency key length at 200 characters; read no more than `MAX_UPLOAD_BYTES + 1`, reusing the constant from `routes/audio.py`. Hash bytes with SHA-256. Check the key before publishing, then enforce the database unique constraint to resolve a concurrent duplicate: same checksum returns the existing record and removes the new file, different checksum returns 409. Register `/search` before `/{meeting_id}` when later routes are added.
- [ ] **Step 4: Run** the Task 2 command and `PYTHONPATH=apps/api uv run python -m unittest apps.api.tests.test_audio_pipeline -v`; expect both suites to pass.
- [ ] **Step 5: Commit** the Task 2 files with `feat: accept local meeting uploads`.

### Task 3: Single Worker and Retry Lifecycle

**Files:** Create `apps/api/app/services/meetings/worker.py`, `apps/api/tests/test_meeting_worker.py`; modify `apps/api/app/services/meetings/repository.py`, `apps/api/app/services/audio/preprocess.py`, `apps/api/app/api/routes/meetings.py`, `apps/api/app/schemas/meeting.py`.

**Interfaces:** Consume `process_audio(data: bytes, filename: str, models: ModelRegistry) -> AudioProcessResponse`. Repository adds `claim_next() -> MeetingRecord | None`, `recover_interrupted() -> None`, `complete(meeting_id: str, result: AudioProcessResponse) -> None`, `fail(meeting_id: str, failure_code: str) -> None`, and `retry(meeting_id: str) -> MeetingRecord | None` (missing ID returns `None`; wrong state raises `MeetingStateConflict`). Worker produces `run_once(repository: MeetingRepository, data_dir: Path, models: ModelRegistry) -> bool` and `main() -> None`, launched by `PYTHONPATH=apps/api uv run python -m app.services.meetings.worker`. Add `AudioTooLongError(InvalidAudioError)` without changing the current HTTP error text.

- [ ] **Step 1: Write failing tests**: two connections cannot claim the same queued job; claim increments attempt count; startup recovers a `processing` job with one claim to `queued` and two claims to `failed`; a failed model load maps to `model_unavailable`; invalid audio maps to `invalid_audio`; overlong audio maps to `audio_too_long`; unexpected exception maps to `processing_error`; no failed job exposes partial segments; retry only accepts `failed`, resets attempts and failure code, preserves ID/audio, and rejects other states with 409; completing a retried job replaces stale result rows and FTS entries atomically; a completed job cannot be claimed again. Patch inference with fakes, never download weights.
- [ ] **Step 2: Run** `PYTHONPATH=apps/api uv run python -m unittest apps.api.tests.test_meeting_worker -v`; expect interface failures.
- [ ] **Step 3: Implement** claim via `BEGIN IMMEDIATE`, ordered by creation time/ID; complete all result rows plus status in one transaction; `fail` clears partial rows. Hold `fcntl.flock(LOCK_EX | LOCK_NB)` on a file under the data directory for the worker lifetime, then call recovery once and reuse one `ModelRegistry` across jobs. Catch known audio/model errors and a final unexpected exception at the worker boundary, logging only meeting ID and safe code. Add `POST /api/meetings/{meeting_id}/retry` with 404/409 mapping.
- [ ] **Step 4: Run** Task 3 tests plus the existing audio API and model-registry tests; expect all to pass.
- [ ] **Step 5: Commit** the Task 3 files with `feat: process queued meetings in local worker`.

### Task 4: List, Detail, Audio Playback, and Deletion

**Files:** Modify `apps/api/app/api/routes/meetings.py`, `apps/api/app/schemas/meeting.py`, `apps/api/app/services/meetings/repository.py`, `apps/api/tests/test_meeting_routes.py`.

**Interfaces:** Repository adds `get_detail(meeting_id: str) -> MeetingDetail | None`, `delete(meeting_id: str) -> str | None` (returns storage key, missing ID returns `None`, processing raises `MeetingStateConflict`), and `count_meetings() -> int`. Schemas expose metadata and, only when completed, `transcript`, `vad_segments`, `diarization_segments`, and `transcript_segments`, reusing existing segment models. List pagination uses `limit=20`, `1..100`, and `offset>=0`; return `{items, total, limit, offset}`.

- [ ] **Step 1: Write failing tests**: list newest first with bounded pagination; detail 404 for missing ID and complete result fields after completion; no partial transcript in queued/failed detail; original audio bytes and `Content-Disposition` download name; valid byte range 206 with exact bytes; unsatisfiable range 416; unknown audio ID 404; deleting queued/completed/failed removes rows, search entries, and audio; processing deletion 409; nonexistent deletion 404. Verify storage keys, not filenames, choose the path.
- [ ] **Step 2: Run** the Task 2 route command; expect new tests to fail.
- [ ] **Step 3: Implement** the listed methods and GET/DELETE routes. Use Starlette `FileResponse` for range requests and a response filename derived safely from stored metadata; resolve only the generated storage key. Delete rows transactionally before unlinking audio; startup orphan cleanup handles a crash between these actions.
- [ ] **Step 4: Run** the route and worker test modules; expect all to pass.
- [ ] **Step 5: Commit** the Task 4 files with `feat: read and delete stored meetings`.

### Task 5: Plain-Text Transcript Search

**Files:** Modify `apps/api/app/services/meetings/repository.py`, `apps/api/app/api/routes/meetings.py`, `apps/api/app/schemas/meeting.py`; create `apps/api/tests/test_meeting_search.py`.

**Interfaces:** Repository adds `search_segments(query: str, limit: int, offset: int) -> tuple[list[SearchHit], int]`. `SearchHit` includes meeting ID, title, speaker, start/end seconds, text. Route `GET /api/meetings/search` returns `{items, total, limit, offset}`; `q` is required, nonblank, at most 200 characters; pagination matches Task 4.

- [ ] **Step 1: Write failing tests**: Vietnamese accented query finds the correct completed meeting and original seconds/speaker; queued/failed records do not appear; no match returns empty list; input such as `" OR *` is plain text and yields no FTS syntax error; pagination is deterministic; blank/overlong query returns 422; deletion and reprocessing remove stale hits; a non-loopback peer is 403.
- [ ] **Step 2: Run** `PYTHONPATH=apps/api uv run python -m unittest apps.api.tests.test_meeting_search -v`; expect route failures.
- [ ] **Step 3: Implement** FTS5 `unicode61` indexing in the same transaction as results. Turn each whitespace-delimited user term into a quoted FTS term by doubling embedded quotes, then join terms with `AND`; pass the expression as a SQL parameter. Order by newest meeting, segment start, and segment ID for stable paging; query only completed meetings. Place `/search` before `/{meeting_id}`.
- [ ] **Step 4: Run** search, routes, and worker test modules; expect all to pass.
- [ ] **Step 5: Commit** the Task 5 files with `feat: search stored transcript segments`.

### Task 6: End-to-End Verification and Operator Docs

**Files:** Create `apps/api/tests/test_meeting_flow.py`; modify `README.md`, `.env.example`, `.gitignore`.

**Interfaces:** Consume all prior tasks; introduce no new runtime interface.

- [ ] **Step 1: Write a local integration test** that posts fixture WAV, observes `queued`, runs one fake-model worker pass, observes `completed` with original-audio timestamps, searches the known Vietnamese text, fetches identical original bytes, then deletes and receives 404. Also assert `/api/audio/process` still returns its existing response fields.
- [ ] **Step 2: Run** `PYTHONPATH=apps/api uv run python -m unittest apps.api.tests.test_meeting_flow -v`; expect failure until any missed wiring is fixed.
- [ ] **Step 3: Complete** wiring and document `MEETING_DATA_DIR`, `uv sync`, loopback API command, worker command, endpoints, retention, stopping worker before a consistent database-plus-audio backup, and rollback that removes routes/schema only after preserving/exporting the data directory. Explain that there is no authentication and no proxy exposure. Keep `.env.example` free of secrets and unignore `docs/superpowers/specs/` and `docs/superpowers/plans/` while ignoring generated meeting data.
- [ ] **Step 4: Run** `PYTHONPATH=apps/api uv run python -m unittest discover -s apps/api/tests -p 'test_*.py'` and `uv run python -m unittest discover -s ai_training/evaluation -p '*_unit.py'`; expect both suites to pass. Inspect `git diff --check` and `git status --short` for generated/private files; no real-model quality claim.
- [ ] **Step 5: Commit** the integration test and docs with `docs: explain local meeting library operation`.

## Self-Review

- Spec coverage: every HTTP route, storage lifecycle, retry rule, privacy constraint, and acceptance test maps to Tasks 1–6.
- Step scan: each task has a failing test, verification, implementation, repeat verification, and scoped commit.
- Type consistency: route and worker methods consume the repository and schema interfaces named where they are introduced.
- Review focus: each of the five listed risks has an explicit test in its owning task.
- Proportion: details are limited to interfaces, fixed values, transaction boundaries, and tests; implementation bodies remain with the executor.
