# Cancel Processing Meetings Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cancel queued or running local meeting jobs immediately while retaining only their metadata and safely removing audio and derived data.

**Architecture:** The supervisor remains the queue claimer, but runs a claimed meeting in a dedicated `multiprocessing` child. It records the child PID and Linux process start ticks before allowing inference to start. Repository cancellation becomes a terminal state transition that clears derived data and returns audio/process metadata; the route deletes audio and only signals a process when start ticks still match.

**Tech Stack:** Python 3.10+, FastAPI/Pydantic, SQLite/WAL, POSIX signals, `multiprocessing`, unittest/httpx.

**Spec:** `docs/superpowers/specs/2026-10-16-cancel-processing-meetings-design.md`

## Global Constraints

- The stateless `POST /api/audio/process` endpoint remains unchanged.
- Keep the local loopback and local-Origin access restrictions.
- Do not expose audio, transcript, cache path, token, or private model errors.
- `cancelled` is terminal; it cannot be claimed or retried.
- Cancellation returns `204` without waiting for child reaping.
- Original audio and derived rows are permanently removed; retained metadata includes filename and pipeline PID.
- Use only POSIX/Linux process controls already assumed by the local worker documentation.

## Review Focus

- Completion racing cancellation leaves a cancelled record with no result rows — Task 1.
- A stale/reused PID is never signalled — Task 2.
- Cancellation before child inference starts does not strand the child — Task 2.
- Existing SQLite stores receive all columns without data loss — Task 1.
- A cancelled meeting exposes metadata but not audio, transcript, or retry — Task 3.

---

### Task 1: Persist cancellation state

**Files:**
- Modify: `apps/api/app/db/session.py`
- Modify: `apps/api/app/services/meetings/repository.py`
- Modify: `apps/api/app/schemas/meeting.py`
- Test: `apps/api/tests/test_meeting_store.py`, `apps/api/tests/test_meeting_worker.py`

**Interfaces:**
- Add `MeetingRecord.pipeline_pid: int | None`, `pipeline_start_ticks: int | None`, and `cancelled_at: str | None`.
- Add `CancelledMeeting(storage_key: str, pipeline_pid: int | None, pipeline_start_ticks: int | None)`.
- Add `set_pipeline_process(meeting_id: str, pid: int, start_ticks: int) -> bool` and `cancel(meeting_id: str) -> CancelledMeeting | None`.

- [ ] Write failing migration/repository tests: initialize a pre-change SQLite file; assert additive columns, no data loss, processing cancellation retains filename/PID, clears all result/FTS rows, and late completion leaves it cancelled.
- [ ] Run `PYTHONPATH=apps/api uv run python -m unittest apps.api.tests.test_meeting_store apps.api.tests.test_meeting_worker`; expect missing schema/API failures.
- [ ] Add `cancelled` to new-store status values, apply missing-column migration using `PRAGMA table_info`, and implement an immediate transaction that clears results but retains the meeting. Make `complete`/ `fail` no-op when the row is cancelled or gone before results are inserted.
- [ ] Re-run the focused tests; expect PASS.
- [ ] Commit only Task 1 files with `feat: persist cancelled meeting state`.

### Task 2: Isolate inference and terminate the active job

**Files:**
- Modify: `apps/api/app/services/meetings/worker.py`
- Test: `apps/api/tests/test_meeting_worker.py`

**Interfaces:**
- Add `process_start_ticks(pid: int) -> int | None` and `terminate_process(pid: int, start_ticks: int) -> bool`.
- Add child target `run_claimed_meeting(meeting_id: str, data_dir: Path, settings: Settings, start_event: Event) -> None`.
- Update `run_once(repository: MeetingRepository, data_dir: Path, models: ModelRegistry) -> bool` to supervise a child and record its process identity before releasing `start_event`.

- [ ] Write failing tests using a real child/Event: PID persistence precedes inference; matching identity terminates; mismatch does not signal; cancellation lets the supervisor claim a second queued meeting.
- [ ] Run `PYTHONPATH=apps/api uv run python -m unittest apps.api.tests.test_meeting_worker`; expect the in-process implementation to fail these tests.
- [ ] Use one child process per claimed meeting. The child waits on the event, reopens SQLite, creates its own `ModelRegistry`, reads the opaque audio, and publishes only while still processing. The parent reads `/proc/<pid>/stat` ticks, stores them before setting the event, and kills a cancelled-before-start child. Send `SIGKILL` only after an exact ticks match. Treat cancelled/missing state as normal exit.
- [ ] Re-run worker tests; expect PASS.
- [ ] Commit only Task 2 files with `feat: isolate meeting inference for cancellation`.

### Task 3: Change DELETE into cancellation

**Files:**
- Modify: `apps/api/app/api/routes/meetings.py`
- Modify: `apps/api/app/schemas/meeting.py`
- Modify: `apps/api/tests/test_meeting_routes.py`
- Modify: `README.md`

**Interfaces:**
- `DELETE /api/meetings/{meeting_id}` returns `204` for every existing state and `404` for an unknown ID.
- Metadata/detail response accepts `status="cancelled"` and includes `pipeline_pid`.

- [ ] Replace the processing-delete conflict test with tests asserting immediate `204`, retained cancelled metadata, absent audio/results, audio-route `404`, retry `409`, and idempotent repeated DELETE.
- [ ] Run `PYTHONPATH=apps/api uv run python -m unittest apps.api.tests.test_meeting_routes apps.api.tests.test_meeting_search`; expect the current `409`/hard delete behavior to fail.
- [ ] Call repository cancellation, unlink the opaque audio with `missing_ok=True`, and call `terminate_process` only for the returned PID/ticks pair. Serialize cancelled metadata and keep detail result-free unless completed. Update README retention/API behavior.
- [ ] Re-run route/search tests; expect PASS.
- [ ] Commit only Task 3 files with `feat: cancel processing meetings from delete API`.

### Task 4: Verify the end-to-end contract

**Files:**
- Modify: `apps/api/tests/test_meeting_flow.py`

**Interfaces:**
- Uses the completed cancellation route and worker supervisor.

- [ ] Write a failing flow test: cancel a blocked running job, upload a second meeting, assert DELETE returns before completion, cancelled metadata has no audio/results, and the next meeting completes.
- [ ] Run `PYTHONPATH=apps/api uv run python -m unittest apps.api.tests.test_meeting_flow`; expect failure until worker/route integration is complete.
- [ ] Make only integration corrections required for the flow; do not add queues, client storage, or dependencies.
- [ ] Run `PYTHONPATH=apps/api uv run python -m unittest discover -s apps/api/tests -p 'test_*.py'`; expect PASS.
- [ ] Commit only Task 4 files with `test: cover cancelled meeting cleanup flow`.

## Self-review

Each spec requirement has an owner: migration/state (Task 1), PID-safe cancellation and worker recovery (Task 2), HTTP contract and documentation (Task 3), and regression integration (Task 4). Interfaces are established before consumption. Review-focus race, migration, PID, and visibility cases are all assigned tests. The plan adds no dependency, queue, frontend, or unrelated persistence.
