CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;

CREATE TABLE meetings (
    id TEXT PRIMARY KEY,
    title TEXT,
    original_filename TEXT NOT NULL,
    storage_key TEXT NOT NULL UNIQUE,
    checksum TEXT NOT NULL,
    idempotency_key TEXT UNIQUE,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('queued', 'processing', 'completed', 'failed', 'cancelled')),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
    duration_seconds double precision CHECK(duration_seconds >= 0 AND duration_seconds < 'Infinity'::float8),
    failure_code TEXT,
    transcript TEXT,
    pipeline_pid INTEGER,
    pipeline_start_ticks bigint,
    cancelled_at timestamptz,
    source TEXT NOT NULL DEFAULT 'upload'
);
CREATE TABLE vad_segments (
    meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
    start_seconds double precision NOT NULL CHECK(start_seconds >= 0),
    end_seconds double precision NOT NULL CHECK(end_seconds > start_seconds AND end_seconds < 'Infinity'::float8),
    PRIMARY KEY (meeting_id, ordinal)
);
CREATE TABLE diarization_segments (
    meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
    start_seconds double precision NOT NULL CHECK(start_seconds >= 0),
    end_seconds double precision NOT NULL CHECK(end_seconds > start_seconds AND end_seconds < 'Infinity'::float8),
    speaker TEXT NOT NULL,
    PRIMARY KEY (meeting_id, ordinal)
);
CREATE TABLE transcript_segments (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
    start_seconds double precision NOT NULL CHECK(start_seconds >= 0),
    end_seconds double precision NOT NULL CHECK(end_seconds > start_seconds AND end_seconds < 'Infinity'::float8),
    speaker TEXT NOT NULL,
    text TEXT NOT NULL,
    document tsvector GENERATED ALWAYS AS (to_tsvector('simple', text)) STORED,
    UNIQUE (meeting_id, ordinal)
);
CREATE TABLE meeting_audio_files (
    meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
    original_filename TEXT NOT NULL,
    storage_key TEXT NOT NULL UNIQUE,
    duration_seconds double precision CHECK(duration_seconds >= 0 AND duration_seconds < 'Infinity'::float8),
    start_seconds double precision CHECK(start_seconds >= 0 AND start_seconds < 'Infinity'::float8),
    PRIMARY KEY (meeting_id, ordinal)
);
CREATE TABLE bot_sessions (
    id TEXT PRIMARY KEY,
    url TEXT,
    url_hash TEXT NOT NULL,
    title TEXT,
    idempotency_key TEXT UNIQUE,
    status TEXT NOT NULL CHECK(status IN ('queued', 'joining', 'waiting_admission', 'recording', 'finalizing', 'completed', 'interrupted', 'cancelled')),
    failure_code TEXT,
    created_at timestamptz NOT NULL,
    started_at timestamptz,
    ended_at timestamptz,
    stop_requested boolean NOT NULL DEFAULT false,
    meeting_id TEXT REFERENCES meetings(id),
    capture_incomplete boolean NOT NULL DEFAULT false
);
CREATE TABLE bot_session_parts (
    session_id TEXT NOT NULL REFERENCES bot_sessions(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
    storage_key TEXT NOT NULL UNIQUE,
    start_seconds double precision NOT NULL CHECK(start_seconds >= 0),
    duration_seconds double precision NOT NULL CHECK(duration_seconds > 0 AND duration_seconds <= 1800),
    PRIMARY KEY (session_id, ordinal)
);
CREATE UNIQUE INDEX one_active_bot_session ON bot_sessions((1))
    WHERE status IN ('queued', 'joining', 'waiting_admission', 'recording', 'finalizing');

CREATE INDEX meetings_created ON meetings(created_at DESC, id DESC);
CREATE TABLE rag_outbox (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 meeting_id text NOT NULL, kind text NOT NULL CHECK(kind IN ('index','purge')),
 revision text, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE storage_identity (
 singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),
 instance_id uuid NOT NULL DEFAULT gen_random_uuid()
);
INSERT INTO storage_identity(singleton) VALUES (true);

CREATE INDEX transcript_document ON transcript_segments USING gin(document);

CREATE TABLE rag_embedding_profiles (
    id text PRIMARY KEY, definition jsonb NOT NULL, ready boolean NOT NULL DEFAULT false
);
CREATE TABLE rag_publications (
    profile_id text REFERENCES rag_embedding_profiles(id) ON DELETE CASCADE,
    meeting_id text NOT NULL REFERENCES meetings(id) ON DELETE CASCADE, revision text NOT NULL, PRIMARY KEY(profile_id, meeting_id)
);
CREATE TABLE rag_chunks (
    profile_id text REFERENCES rag_embedding_profiles(id) ON DELETE CASCADE,
    source_id text NOT NULL, meeting_id text NOT NULL, revision text NOT NULL,
    ordinal integer NOT NULL CHECK(ordinal >= 0), text text NOT NULL, payload jsonb NOT NULL,
    document tsvector GENERATED ALWAYS AS (to_tsvector('simple', text)) STORED,
    PRIMARY KEY(profile_id, source_id),
    FOREIGN KEY(profile_id, meeting_id) REFERENCES rag_publications(profile_id, meeting_id) ON DELETE CASCADE
);
CREATE INDEX rag_chunks_document ON rag_chunks USING gin(document);
CREATE INDEX rag_chunks_meeting ON rag_chunks(profile_id, meeting_id);
CREATE TABLE rag_chunk_embeddings (
    profile_id text NOT NULL, source_id text NOT NULL, embedding vector NOT NULL,
    dimensions integer NOT NULL CHECK(dimensions > 0),
    PRIMARY KEY(profile_id, source_id),
    FOREIGN KEY(profile_id, source_id) REFERENCES rag_chunks(profile_id, source_id) ON DELETE CASCADE,
    CHECK(vector_dims(embedding) = dimensions)
);
CREATE TABLE rag_index_jobs (
    profile_id text REFERENCES rag_embedding_profiles(id) ON DELETE CASCADE,
    meeting_id text NOT NULL REFERENCES meetings(id) ON DELETE CASCADE, revision text NOT NULL, state text NOT NULL DEFAULT 'pending',
    attempts integer NOT NULL DEFAULT 0, failure_code text, lease_until timestamptz,
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(profile_id, meeting_id)
);
