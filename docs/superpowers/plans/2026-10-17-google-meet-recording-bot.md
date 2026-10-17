# Google Meet Recording Bot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let one local Chrome bot join a pasted Google Meet link, record ordered audio parts under 30 minutes each, and create one meeting for the existing AI worker.

**Architecture:** A local-only API stores durable bot sessions in the existing SQLite database. A separate bot runner owns Chrome and a dedicated PulseAudio-compatible output, publishes finalized WAV parts, and creates one meeting after capture ends; the existing worker processes those parts with persisted timeline offsets.

**Tech Stack:** Python 3.10+, FastAPI/Pydantic, SQLite, uv, Playwright Python, installed Google Chrome, PulseAudio/PipeWire Pulse compatibility, FFmpeg, React/TypeScript.

**Spec:** `docs/superpowers/specs/2026-10-17-google-meet-recording-bot-design.md`

## Global Constraints

- Google Meet only; one active bot session per local Linux machine.
- Bot uses a private Chrome profile and the visible name `Meeting AI Recorder`; login is interactive and credentials are never stored by the app.
- Capture 16 kHz mono PCM WAV through a dedicated audio output; never record the default system output.
- Roll over before 1,800 decoded seconds per part; preserve timeline gaps and keep one meeting ID.
- Do not auto-rejoin; retain finalized parts after interruption and offer explicit process or discard actions.
- Keep `POST /api/meetings`, `/api/audio/process`, upload limits, local-only access, and existing response fields compatible.
- Do not log full Meet URLs, cookies, audio, transcripts, or private paths.
- Preserve unrelated workspace edits, including any existing `.env.example` changes; stage only bot-specific lines when committing.

## Review Focus

- A lookalike URL such as `meet.google.com.evil` must be rejected before storage (Task 3 route test).
- A stop while waiting for host admission must leave no meeting or audio (Task 6 runner test).
- Two near-simultaneous start requests must leave at most one active session (Task 2 repository test).
- A crash after file publication must not let orphan cleanup delete a registered part or retain an unregistered file (Task 2 storage test).
- A gap between recorded parts must remain in transcript timestamps and audio seek positions (Tasks 1 and 7 tests/checks).

---

### Task 1: Meeting source and timeline offsets

**Files:** Modify `apps/api/app/db/session.py`, `apps/api/app/services/meetings/repository.py`, `apps/api/app/services/meetings/worker.py`, `apps/api/app/schemas/meeting.py`, `apps/api/app/api/routes/meetings.py`; test `apps/api/tests/test_meeting_store.py`, `apps/api/tests/test_meeting_worker.py`, `apps/api/tests/test_meeting_routes.py`.

**Interfaces:** Produce `MeetingRecord.source: str`, `MeetingAudioFile.start_seconds: float | None`, and `process_meeting_parts(parts: list[MeetingAudioFile], data_dir: Path, models: ModelRegistry, source: str = "upload") -> tuple[AudioProcessResponse, list[float]]`. Extend `MeetingRepository.create_meeting(..., audio_files=..., source: str = "upload")` without changing existing callers.

- [ ] **Step 1: Write failing tests.** Add `test_bot_source_preserves_part_gap_and_exceeds_thirty_minutes` using two fake 1,000-second results at starts `0` and `1,200`; assert second transcript starts at `1,200`, result duration is `2,200`, and source is `google_meet_bot`. Add `test_upload_source_still_rejects_combined_duration_over_thirty_minutes` and a migration test asserting old rows read as `upload`.
- [ ] **Step 2: Verify red.** Run `PYTHONPATH=apps/api uv run python -m unittest apps.api.tests.test_meeting_worker apps.api.tests.test_meeting_store`; expect the new tests to fail on missing source/offset support.
- [ ] **Step 3: Implement.** Add additive `source TEXT NOT NULL DEFAULT 'upload'` and nullable `start_seconds REAL` migrations. Store bot offsets, derive upload offsets from preceding decoded durations, expose `source` in meeting list/detail and `start_seconds` in detail audio files. Pass `meeting.source` from `run_claimed_meeting` into `process_meeting_parts`; retain the aggregate limit only for `upload`. Bot duration is the maximum part end, not the sum.
- [ ] **Step 4: Verify green and compatibility.** Run `PYTHONPATH=apps/api uv run python -m unittest apps.api.tests.test_meeting_worker apps.api.tests.test_meeting_store apps.api.tests.test_meeting_routes`; expect zero failures.
- [ ] **Step 5: Commit.** Commit only the files above with `feat: add source-aware meeting timelines`.

### Task 2: Durable bot sessions and atomic finalization

**Files:** Modify `apps/api/app/db/session.py`, `apps/api/app/services/meetings/storage.py`, `apps/api/app/services/meetings/repository.py`; create `apps/api/app/services/meetings/bot_repository.py`; test `apps/api/tests/test_bot_repository.py`, `apps/api/tests/test_meeting_store.py`.

**Interfaces:** `BotSessionRepository(connection: sqlite3.Connection)` produces `create(url: str, title: str | None, key: str | None) -> BotSession`, `claim_next() -> BotSession | None`, `get(session_id: str) -> BotSession | None`, `get_active() -> BotSession | None`, `request_stop(session_id: str) -> BotSession | None`, `set_status(session_id: str, status: str, failure_code: str | None = None) -> BotSession`, `add_part(session_id: str, storage_key: str, start_seconds: float, duration_seconds: float) -> BotPart`, `finalize(session_id: str, partial: bool = False) -> MeetingRecord`, `discard(session_id: str) -> tuple[str, ...]`, and `recover_stale() -> int`. Session and part dataclasses live in this module. Add `publish_audio_file(data_dir: Path, temporary_path: Path) -> str` in `storage.py`.

- [ ] **Step 1: Write failing tests.** `test_concurrent_create_allows_one_active_session` uses two SQLite connections and asserts one success plus one state conflict. `test_reused_key_with_different_link_conflicts` prevents accidental replay of another URL. `test_finalization_is_idempotent_and_moves_references_atomically` asserts one meeting, ordered files, cleared URL, and no duplicate meeting on replay. `test_partial_finalization_marks_capture_incomplete` checks the stored flag. `test_cleanup_keeps_registered_session_part_and_removes_unregistered_file` covers publication/recovery. Add tests for stop, discard, stale recovery, and private directory/file modes.
- [ ] **Step 2: Verify red.** Run `PYTHONPATH=apps/api uv run python -m unittest apps.api.tests.test_bot_repository`; expect missing module/tests to fail.
- [ ] **Step 3: Implement.** Add `bot_sessions` (including `capture_incomplete BOOLEAN NOT NULL DEFAULT 0`) and `bot_session_parts` tables with a partial unique index over active states and unique optional idempotency key. Use `BEGIN IMMEDIATE` for transitions and finalization. Register each published part while holding `audio_publication_lock`; finalization computes the meeting checksum by streaming the ordered audio bytes with the existing length-delimited multi-part rule, inserts the meeting and `meeting_audio_files` with offsets/durations, updates the session, and removes session-part rows in one transaction. Extend orphan cleanup to reference both part tables; unregistered files remain eligible for cleanup. `discard` may affect only sessions without a resulting meeting and returns file keys for locked deletion.
- [ ] **Step 4: Verify green.** Run `PYTHONPATH=apps/api uv run python -m unittest apps.api.tests.test_bot_repository apps.api.tests.test_meeting_store`; expect zero failures.
- [ ] **Step 5: Commit.** Commit the persistence files and tests with `feat: persist bot sessions and audio parts`.

### Task 3: Local bot-session API

**Files:** Create `apps/api/app/schemas/bot_session.py`, `apps/api/app/api/routes/bot_sessions.py`, `apps/api/tests/test_bot_session_routes.py`; modify `apps/api/app/main.py`.

**Interfaces:** Expose `POST /api/bot-sessions`, `GET /api/bot-sessions/active`, `GET /api/bot-sessions/{id}`, `POST /api/bot-sessions/{id}/stop`, `POST /api/bot-sessions/{id}/process-parts`, and `DELETE /api/bot-sessions/{id}`. Create body is `{ "url": str, "title": str | null }`; status response includes `id`, `status`, `failure_code`, `created_at`, `started_at`, `ended_at`, `finalized_part_count`, `capture_incomplete`, and `meeting_id`. The API calls Task 2's repository and reuses `require_loopback` from `meetings.py`.

- [ ] **Step 1: Write failing tests.** `test_create_accepts_canonical_meet_url_and_strips_query` checks `202` and stored canonical URL. `test_create_rejects_lookalike_host_and_noncanonical_path` checks `422` and no row for `https://meet.google.com.evil/abc-defg-hij`, `http://meet.google.com/abc-defg-hij`, and `/lookup/...`. Add status/stop/partial-finalize/discard/idempotency/second-active tests plus remote-peer and foreign-Origin `403` tests.
- [ ] **Step 2: Verify red.** Run `PYTHONPATH=apps/api uv run python -m unittest apps.api.tests.test_bot_session_routes`; expect missing route or incorrect status.
- [ ] **Step 3: Implement.** Validate exact HTTPS host and canonical three-four-three lowercase code before storage; strip query and fragment. Map state conflicts to `409`, invalid input to `422`, missing session to `404`. Never return stored URL or raw exception text. For a completed session derive `finalized_part_count` from its meeting audio rows; for an active session derive it from session parts. Reject `process-parts` unless the session is interrupted with at least one finalized part, and reject discard after a meeting was created.
- [ ] **Step 4: Verify green.** Run `PYTHONPATH=apps/api uv run python -m unittest apps.api.tests.test_bot_session_routes apps.api.tests.test_meeting_routes`; expect zero failures.
- [ ] **Step 5: Commit.** Commit API files and tests with `feat: expose local bot session controls`.

### Task 4: Dedicated audio capture and local readiness

**Files:** Create `apps/api/app/services/meetings/bot_capture.py`, `apps/api/tests/test_bot_capture.py`; modify `apps/api/app/core/config.py` and bot-specific lines in `.env.example`.

**Interfaces:** `check_bot_runtime(settings: Settings) -> None` verifies Chrome executable, FFmpeg, and an explicitly configured PulseAudio-compatible sink/monitor. `PulseWavRecorder(settings: Settings).record_part(temporary_path: Path, stop: asyncio.Event, max_seconds: int = 1790) -> RecordedPart` returns decoded duration and monotonic start/end times; it raises `CaptureUnavailable` for safe runner mapping. Settings add `BOT_CHROME_PATH: Path | None`, `BOT_AUDIO_SINK: str | None`, `BOT_ADMISSION_TIMEOUT_SECONDS: int = 300`, and a private bot profile directory derived from `MEETING_DATA_DIR`.

- [ ] **Step 1: Write failing tests.** `test_runtime_rejects_missing_dedicated_sink_without_default_fallback` checks preflight failure before FFmpeg starts. `test_recorder_invokes_ffmpeg_for_pulse_monitor_and_pcm_wav` checks the command argument list includes the configured monitor, `-ar 16000`, `-ac 1`, `pcm_s16le`, and `-t 1790`. Test graceful stop finalizes a decodable short WAV; forced-kill output is not published.
- [ ] **Step 2: Verify red.** Run `PYTHONPATH=apps/api uv run python -m unittest apps.api.tests.test_bot_capture`; expect missing module.
- [ ] **Step 3: Implement.** Use argument arrays, not a shell; check the configured monitor exists and never substitute the default output. Stop FFmpeg gracefully, inspect decoded length, and discard files with zero samples or more than 1,800 seconds. Keep subprocess stderr private and map failure codes without raw log content. Add only the settings required by this interface; do not change model defaults.
- [ ] **Step 4: Verify green.** Run `PYTHONPATH=apps/api uv run python -m unittest apps.api.tests.test_bot_capture apps.api.tests.test_meeting_store`; expect zero failures.
- [ ] **Step 5: Commit.** Commit capture, bot-specific config/settings changes, and tests with `feat: capture isolated Meet audio parts`.

### Task 5: Chrome Meet participant adapter

**Files:** Create `apps/api/app/services/meetings/meet_browser.py`, `apps/api/tests/test_meet_browser.py`; modify root `pyproject.toml` and `uv.lock` to add `playwright` through `uv`.

**Interfaces:** `ChromeMeetBrowser(settings: Settings)` exposes `async start(url: str) -> None`, `async wait_for_admission(timeout_seconds: int, on_waiting: Callable[[], None]) -> None`, `async wait_for_end() -> None`, and `async close() -> None`. It uses the dedicated profile and Chrome executable from Task 4; the runner in Task 6 controls recording and state changes.

- [ ] **Step 1: Write failing tests.** With a fake Playwright page, `test_join_disables_mic_camera_and_waits_for_admission` checks no recording signal before admission. `test_account_challenge_or_timeout_reports_join_failure` checks bounded failure. `test_browser_end_and_close_release_context` checks cleanup. No test launches Chrome or accesses Google.
- [ ] **Step 2: Verify red.** Run `PYTHONPATH=apps/api uv run python -m unittest apps.api.tests.test_meet_browser`; expect missing module.
- [ ] **Step 3: Implement.** Launch installed Chrome headfully with a persistent private profile and a dedicated `PULSE_SINK` environment setting. Set the bot display name when Meet permits it, turn off mic/camera in prejoin, recognize waiting/admitted/ended states with explicit timeouts, and close browser resources in `finally`. Do not automate Google password entry or bypass account challenges.
- [ ] **Step 4: Verify green.** Run `PYTHONPATH=apps/api uv run python -m unittest apps.api.tests.test_meet_browser`; expect zero failures.
- [ ] **Step 5: Commit.** Commit adapter, `pyproject.toml`, `uv.lock`, and tests with `feat: join Google Meet with bot Chrome profile`.

### Task 6: Bot runner and recovery

**Files:** Create `apps/api/app/services/meetings/bot_runner.py`, `apps/api/tests/test_bot_runner.py`; modify `apps/api/app/services/meetings/storage.py` if session-aware cleanup needs a focused helper.

**Interfaces:** `async run_session(session_id: str, data_dir: Path, settings: Settings, browser_factory: Callable[..., ChromeMeetBrowser], recorder_factory: Callable[..., PulseWavRecorder]) -> None`; `main() -> None` is the third local process entry point. It consumes Task 2's repository, Task 4's recorder, and Task 5's browser.

- [ ] **Step 1: Write failing tests.** `test_waiting_stop_creates_no_meeting_or_audio`, `test_recorded_parts_finalize_to_one_meeting`, `test_browser_exit_keeps_completed_parts_and_marks_interrupted`, and `test_restart_recovery_does_not_rejoin` use fake browser/recorder interfaces. Add a test that two runner processes cannot hold the file lock and that a stop request closes the active recorder.
- [ ] **Step 2: Verify red.** Run `PYTHONPATH=apps/api uv run python -m unittest apps.api.tests.test_bot_runner`; expect missing runner.
- [ ] **Step 3: Implement.** Hold a host-level lock for one active session. Before joining, run `check_bot_runtime`; after admission, record parts sequentially, publish/register each closed part under the publication lock, and compute offsets from monotonic capture times. Observe browser end and durable stop requests. On normal stop finalize once; on errors mark interrupted with a safe code and retain finalized parts. On startup recover stale active states, then claim queued sessions.
- [ ] **Step 4: Verify green.** Run `PYTHONPATH=apps/api uv run python -m unittest apps.api.tests.test_bot_runner apps.api.tests.test_bot_repository apps.api.tests.test_meeting_worker`; expect zero failures.
- [ ] **Step 5: Commit.** Commit runner and tests with `feat: supervise Google Meet recording sessions`.

### Task 7: Web controls and timeline playback

**Files:** Create `apps/web/src/components/MeetBotPanel.tsx`, `apps/web/src/lib/botApi.ts`, `apps/web/src/lib/timeline.ts`, `apps/web/src/lib/timeline.test.ts`; modify `apps/web/src/App.tsx`, `apps/web/src/lib/api.ts`, `apps/web/src/types/meeting.ts`, `apps/web/package.json`, `apps/web/package-lock.json`, and the relevant existing CSS file.

**Interfaces:** `botApi.create(url: string, title: string)`, `botApi.active()`, `botApi.get(id: string)`, `botApi.stop(id: string)`, `botApi.processParts(id: string)`, and `botApi.discard(id: string)` match Task 3. `ApiMeeting.source` and detail `audio_files[].start_seconds` match Task 1. `findAudioPartAtTime(parts: {start_seconds: number; duration_seconds: number}[], seconds: number): number | null` returns the selected ordinal or `null` for a gap.

- [ ] **Step 1: Write the failing test.** Add `timeline.test.ts` cases for parts `[0, 1000]` and `[1200, 1000]`: `findAudioPartAtTime(..., 1000)` and `(..., 1100)` return `null`; `(..., 1200)` returns the second ordinal. Add `vitest@1.6.1` (compatible with this repo's Node 18 and Vite 5) and `"test": "vitest run"` to `package.json`.
- [ ] **Step 2: Verify red.** Run `npm test` in `apps/web`; expect failure because `findAudioPartAtTime` is not implemented.
- [ ] **Step 3: Implement.** Add the timeline helper, a Meet entry point beside upload, an active-session panel with state-specific controls, and a recording notice. Poll only while a session is active, clean up polling on unmount, and show safe API messages. Use the helper for transcript seeking and do not play a neighboring part for a gap.
- [ ] **Step 4: Verify green and UI behavior.** Run `npm test`, `npm run typecheck`, and `npm run build` in `apps/web`; expect all to exit 0. Manually verify URL submission, admission/recording states, part count, stop, interrupted-session actions, one-meeting navigation, keyboard focus, and API error display with local or stubbed API responses.
- [ ] **Step 5: Commit.** Commit web files and lockfile with `feat(web): control local Meet recording bot`.

### Task 8: Setup, full verification, and operator smoke gate

**Files:** Modify `README.md`.

**Interfaces:** Document `PYTHONPATH=apps/api uv run --env-file .env python -m app.services.meetings.bot_runner`, installed Chrome path, dedicated PulseAudio-compatible sink/monitor, private profile, API and inference worker processes, and setup of the bot Google account.

- [ ] **Step 1: Document.** Explain local Linux requirements, manual bot-account sign-in by opening installed Chrome with the private `MEETING_DATA_DIR/bot-profile` user-data directory and closing it before the runner starts, host admission, participant recording notice, 30-minute WAV parts, storage growth, safe failure states, deletion/retention, migration, and rollback. Verify the profile and generated captures remain under the ignored meeting data directory; never copy real tokens or recordings into source control.
- [ ] **Step 2: Run full fast verification.** Run `PYTHONPATH=apps/api uv run python -m unittest discover -s apps/api/tests -p 'test_*.py'`, `uv run python -m unittest discover -s ai_training/evaluation -p '*_unit.py'`, then `npm test`, `npm run typecheck`, and `npm run build` in `apps/web`. Report any failures by name.
- [ ] **Step 3: Run local acceptance when the target machine is ready.** With an approved test Meet, verify admission, private audio capture, short and accelerated-rollover recordings, one finalized meeting, stop, interruption, and deletion. If Chrome, the dedicated sink, a bot account, or a consenting test meeting is unavailable, record the missing gate explicitly; do not claim real Meet capture works.
- [ ] **Step 4: Commit.** Commit only documentation/config changes with `docs: explain local Meet bot setup and recovery`.

## Execution handoff

Review this plan and the linked spec before implementation. Native execution is recommended because the tasks have tightly coupled database, runner, and API interfaces; complete them in order with TDD and focused commits. The target-machine smoke gate remains required before calling live Meet recording operational.
