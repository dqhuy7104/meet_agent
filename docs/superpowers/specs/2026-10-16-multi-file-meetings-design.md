# Multiple audio files in one meeting

The upload dialog sends repeated `files` fields in selection order. The local
meeting API also accepts the original single `file` field. One request creates
one queued meeting; each source is kept in private storage with its ordinal and
filename in `meeting_audio_files`. Existing meetings are backfilled as part 0.

The worker processes parts sequentially with one model registry per meeting.
Durations shift all VAD, diarization, and transcript segments onto one meeting
timeline. Later parts get distinct speaker labels because independent files do
not establish cross-file speaker identity. The combined duration remains capped
at 30 minutes. Up to 10 files can be uploaded, with each at most 25 MiB and
the request at most 100 MiB.

Meeting detail exposes ordered `audio_files` and their decoded durations.
`/audio` continues serving part 0; `/audio/{ordinal}` serves each source with
range support. The web client renders one player per file and maps transcript
seeks to the corresponding player's local time. Cancellation removes every
source and derived result. Legacy single-file clients and meetings keep their
existing behavior.
