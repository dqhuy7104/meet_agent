# Cancel processing meetings

## Goal

Allow a local user to cancel a queued or running meeting so a single worker can
start another meeting. Cancellation returns immediately, retains minimal meeting
metadata, and permanently removes the original audio and job temporary files.

## Scope

This changes the local meeting library only. The stateless `POST
/api/audio/process` endpoint and its response contract are unchanged.

## Data model

Add `cancelled` to the meeting status values. Add nullable `pipeline_pid` and
`cancelled_at` fields to `meetings`. `pipeline_pid` is the PID of the dedicated
process currently or most recently assigned to the meeting. It is audit
metadata after cancellation, not an authority to signal a process later.

Existing SQLite stores are migrated at initialization with additive `ALTER
TABLE` statements. New stores create the columns directly. A cancelled meeting
has no audio, transcript, VAD, diarization, transcript segments, or FTS rows.

## Worker and cancellation lifecycle

The worker supervisor claims one queued meeting and executes its inference in a
dedicated child process. It records a process identity before inference starts.
The child owns audio decoding, model loading, and result publication. The
supervisor waits for that child before claiming the next meeting.

`DELETE /api/meetings/{id}` accepts queued, processing, completed, failed, and
cancelled meetings. For a processing meeting, the route atomically records the
cancelled terminal state and clears all derived rows before attempting to stop
the recorded child process. It unlinks the original audio and job temporary
files, then returns `204 No Content` without waiting for the supervisor to
reap the child. The route uses the recorded process identity only while the
meeting is transitioning to cancelled, preventing a later PID reuse from being
signalled.

The child or supervisor treats a missing/cancelled meeting as expected
cancellation: it does not publish a completed result, mark the meeting failed,
or log private inference input. On startup the worker clears stale active-job
records and job temporary directories left by an interrupted process. A
cancelled meeting is never retried or claimed again.

## API contract

The existing delete route remains `DELETE /api/meetings/{id}` with `204` on
success and `404` for an unknown ID. It now cancels processing work instead of
returning `409`. List and detail responses expose `status: "cancelled"` and
the retained `pipeline_pid`; they do not expose an audio URL or transcript for
cancelled meetings. Retrying a cancelled meeting returns `409` because its
audio no longer exists.

## Error handling

Database transition errors are mapped to the existing safe HTTP failure path.
If process termination races with normal completion, cancellation wins once its
transaction commits; a late child publication becomes a no-op. If an audio or
temporary file is already absent, cancellation remains successful. The worker
continues serving subsequent queued meetings after a child exits due to
cancellation.

## Tests and documentation

Focused repository and route tests cover the new terminal state, schema
migration, cancelled visibility, audio/result cleanup, no retry, and deletion
of a running child. Worker tests verify that cancellation does not turn into a
failed/completed record and that the next queued meeting can run. README
documents the immediate response, retained metadata, permanent audio removal,
and model reload implication after cancelling a running child.

## Operational consequence

Killing a dedicated job process releases its model memory immediately. The
next job loads models in its new process, so it may have model-startup latency.
This is the trade-off required for reliable interruption of blocking inference.
