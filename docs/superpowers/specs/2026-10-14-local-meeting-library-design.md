# Local meeting library API

## Goal and scope

Build a single-user, single-machine API that keeps uploaded meeting audio and its processed transcript. A caller can upload a recording, poll its processing state, list meetings, read the transcript and speaker turns, search transcript text, fetch the original audio, and delete a meeting. There is no web client, account system, cloud storage, summarization, embedding, or chat in this release.

The existing `POST /api/audio/process` response and behavior remain unchanged. The new workflow uses the existing FFmpeg, VAD, language-routed ASR, diarization, and speaker assignment pipeline. It does not change model settings or preprocessing.

## Architecture

- `apps/api/app/api/routes` owns upload limits, input validation, response schemas, status codes, and local-only access to the new meeting routes.
- `apps/api/app/services` owns meeting persistence, audio file storage, and text search. SQLite holds meeting metadata, job state, VAD regions, diarization turns, transcript segments, and the search index. Original audio lives in a configurable local data directory under an opaque generated name.
- A separate worker process on the same machine claims one queued meeting at a time and calls the existing `process_audio` pipeline. It keeps the model registry alive between jobs. The API process does not run model inference for the new meeting workflow.
- The worker and API share SQLite and the audio directory. No Redis, external database, provider, or object store is required. The worker holds an exclusive local lock so only one instance runs; job claims are transactional.

The documented launch binds the API to `127.0.0.1`. The new routes reject non-loopback clients. This local-only release has no account login or authorization model and must not be exposed through a network proxy. The data directory is created with owner-only permissions. Audio and transcript contents are not logged.

## Data model and lifecycle

Each meeting has a generated ID, optional title, original filename, storage key, creation and update timestamps, status, attempt count, duration when known, and a safe failure code when processing fails. Statuses are `queued`, `processing`, `completed`, and `failed`. Segments retain seconds relative to the uploaded audio. Transcript segments keep text and the speaker with greatest overlap; diarization turns keep all overlaps. VAD regions are also stored so the completed record can represent the current `AudioProcessResponse` data.

The upload route enforces the current 25 MiB limit and rejects empty uploads. It writes audio using a temporary file and publishes it under a generated storage key before queuing the meeting. A failed database insert removes the published file; startup cleanup removes unreferenced files left by a process crash. FFmpeg validity and the 30-minute duration limit are checked by the worker; invalid or overlong audio ends in `failed` with a safe error code. No partial transcript is visible: the worker commits all result rows and `completed` in one database transaction. If processing fails, the audio stays available for inspection or retry.

On worker startup, an interrupted `processing` job is queued again if it has been claimed fewer than two times; otherwise it becomes `failed`. Model and decoding failures become `failed` without automatic retry. A manual retry of a failed meeting requeues the same ID and audio and resets the interruption count. Reprocessing replaces result rows and search entries atomically, so retry cannot duplicate transcript segments. The first successful completion ends the job.

Data is retained until the caller deletes the meeting. Deletion removes database rows, search entries, and the audio file. Deleting a `processing` meeting returns `409`; queued, completed, and failed meetings may be deleted. Startup cleanup removes abandoned temporary upload files. Deployment instructions include backing up the SQLite database and audio directory together, and describe how to remove this feature by stopping the worker and reverting the added routes and schema while retaining or exporting the local data directory.

## HTTP contract

| Method and path | Behavior |
| --- | --- |
| `POST /api/meetings` | Multipart audio plus optional title; returns `202` with meeting ID and `queued` status. Empty upload is `400`; oversized upload is `413`. A new request creates a new meeting. |
| `GET /api/meetings` | Paginated list of metadata and status, newest first. |
| `GET /api/meetings/{id}` | Metadata, status, and, when completed, transcript, VAD regions, diarization turns, and transcript segments. Unknown ID is `404`. |
| `GET /api/meetings/{id}/audio` | Serves the stored original audio, including byte-range requests for playback. Unknown ID is `404`. |
| `GET /api/meetings/search?q=...` | Paginated matching transcript segments from completed meetings, with meeting ID, title, speaker, start/end seconds, and text. Empty matches return an empty list. |
| `POST /api/meetings/{id}/retry` | Requeues only a failed meeting; other states return `409`. |
| `DELETE /api/meetings/{id}` | Deletes a non-processing meeting and its audio; processing returns `409`. |

An optional `Idempotency-Key` on upload allows a client to safely repeat a request: the same key and audio checksum return the original meeting ID and metadata; reusing the key with different audio returns `409`. Without the header, each upload creates a new meeting. All list and search endpoints have bounded pagination. The search route accepts plain text only, limits query length, and escapes FTS syntax before querying SQLite FTS5. Initial search covers Unicode terms as written, including Vietnamese with accents; accent-free equivalents are not a release requirement.

Responses expose safe failure codes such as `invalid_audio`, `audio_too_long`, and `model_unavailable`, without model tokens, internal paths, stack traces, or recording contents. Server errors do not leave a meeting stuck in `processing`. Paths are resolved only from stored generated keys, never from client filenames.

## Verification and acceptance

- API tests use fake inference models and real FFmpeg decoding where relevant. They cover upload size and emptiness, invalid and overlong audio, list and detail contracts, original-audio retrieval, deletion, and local-only access.
- Worker tests cover one claim per job, state transitions, an interrupted process restart, the two-claim limit, model failure, manual retry, and atomic replacement of segments and search entries.
- Search tests use Vietnamese text and verify the returned meeting ID, speaker, and original-audio timestamps. They also cover empty results, malformed FTS-like input treated as plain text, and pagination.
- A small local end-to-end test uploads fixture audio, runs a fake-model worker, searches the completed transcript, fetches audio, and deletes the meeting. Tests do not download model weights or require tokens.
- README documents data directory configuration, loopback-only API launch, worker launch, storage/retention, backup and rollback, and the new API contract. `.env.example` includes new nonsecret settings. No real-model quality or latency claim is made without a separate evaluation run.
