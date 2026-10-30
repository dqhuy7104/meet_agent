CREATE TABLE meeting_summaries (
    meeting_id text PRIMARY KEY REFERENCES meetings(id) ON DELETE CASCADE,
    transcript_revision text NOT NULL,
    state text NOT NULL CHECK(state IN ('generating', 'ready')),
    claim_id uuid NOT NULL,
    claimed_at timestamptz NOT NULL DEFAULT now(),
    summary text,
    provider_id text,
    model_id text,
    prompt_version text,
    created_at timestamptz,
    CHECK ((state = 'generating' AND summary IS NULL AND provider_id IS NULL AND model_id IS NULL AND prompt_version IS NULL AND created_at IS NULL)
        OR (state = 'ready' AND summary IS NOT NULL AND provider_id IS NOT NULL AND model_id IS NOT NULL AND prompt_version IS NOT NULL AND created_at IS NOT NULL))
);
