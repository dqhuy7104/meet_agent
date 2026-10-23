CREATE TABLE voice_profiles (
    id text PRIMARY KEY DEFAULT gen_random_uuid()::text,
    display_name text NOT NULL CHECK(length(display_name) BETWEEN 1 AND 100 AND display_name = btrim(display_name)),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE voice_samples (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    profile_id text NOT NULL REFERENCES voice_profiles(id) ON DELETE CASCADE,
    embedding vector NOT NULL,
    dimensions integer NOT NULL CHECK(dimensions > 0),
    fingerprint text NOT NULL,
    source text NOT NULL CHECK(source IN ('upload', 'meeting')),
    source_meeting_id text REFERENCES meetings(id) ON DELETE CASCADE,
    source_speaker text,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK(vector_dims(embedding) = dimensions),
    CHECK((source = 'upload' AND source_meeting_id IS NULL AND source_speaker IS NULL)
       OR (source = 'meeting' AND source_meeting_id IS NOT NULL AND source_speaker IS NOT NULL))
);
CREATE INDEX voice_samples_profile ON voice_samples(profile_id, id);
CREATE INDEX voice_samples_meeting ON voice_samples(source_meeting_id);
CREATE TABLE voice_enrollment_jobs (
    id text PRIMARY KEY DEFAULT gen_random_uuid()::text,
    profile_id text NOT NULL REFERENCES voice_profiles(id) ON DELETE CASCADE,
    source text NOT NULL CHECK(source IN ('upload', 'meeting')),
    upload_key text,
    source_meeting_id text REFERENCES meetings(id) ON DELETE CASCADE,
    source_speaker text,
    state text NOT NULL DEFAULT 'queued' CHECK(state IN ('queued', 'processing', 'completed', 'failed')),
    failure_code text,
    attempt_count integer NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK((source = 'upload' AND upload_key IS NOT NULL AND source_meeting_id IS NULL AND source_speaker IS NULL)
       OR (source = 'meeting' AND upload_key IS NULL AND source_meeting_id IS NOT NULL AND source_speaker IS NOT NULL))
);
CREATE INDEX voice_enrollment_jobs_queue ON voice_enrollment_jobs(created_at, id) WHERE state = 'queued';
CREATE TABLE meeting_speaker_identities (
    meeting_id text NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    speaker text NOT NULL,
    profile_id text REFERENCES voice_profiles(id) ON DELETE SET NULL,
    score double precision CHECK(score BETWEEN -1 AND 1),
    origin text NOT NULL CHECK(origin IN ('automatic', 'manual')),
    fingerprint text,
    PRIMARY KEY(meeting_id, speaker),
    CHECK(origin <> 'automatic' OR fingerprint IS NOT NULL)
);
ALTER TABLE meetings ADD COLUMN voice_identification_status text NOT NULL DEFAULT 'not_run'
    CHECK(voice_identification_status IN ('not_run', 'completed', 'failed', 'disabled'));
