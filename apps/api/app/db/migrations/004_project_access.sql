CREATE TABLE accounts (
    id text PRIMARY KEY DEFAULT gen_random_uuid()::text,
    email text NOT NULL CHECK (email = lower(btrim(email)) AND email <> ''),
    password_hash text NOT NULL,
    is_system_admin boolean NOT NULL DEFAULT false,
    is_disabled boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (email)
);

CREATE TABLE projects (
    id text PRIMARY KEY DEFAULT gen_random_uuid()::text,
    name text NOT NULL CHECK (name = btrim(name) AND name <> ''),
    is_archived boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE project_memberships (
    project_id text NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    account_id text NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    role text NOT NULL CHECK (role IN ('project_manager', 'member', 'viewer')),
    PRIMARY KEY (project_id, account_id)
);
CREATE INDEX project_memberships_account ON project_memberships(account_id, project_id);

ALTER TABLE meetings
    ADD COLUMN project_id text REFERENCES projects(id),
    ADD COLUMN uploaded_by_account_id text REFERENCES accounts(id),
    ADD COLUMN legacy_unassigned boolean NOT NULL DEFAULT false;
UPDATE meetings SET legacy_unassigned = true;
ALTER TABLE meetings ADD CONSTRAINT meetings_project_required
    CHECK (project_id IS NOT NULL OR legacy_unassigned);
CREATE INDEX meetings_project_created ON meetings(project_id, created_at DESC, id DESC);

ALTER TABLE bot_sessions
    ADD COLUMN project_id text REFERENCES projects(id),
    ADD COLUMN started_by_account_id text REFERENCES accounts(id),
    ADD COLUMN legacy_unassigned boolean NOT NULL DEFAULT false;
UPDATE bot_sessions SET legacy_unassigned = true;
ALTER TABLE bot_sessions ADD CONSTRAINT bot_sessions_project_required
    CHECK (project_id IS NOT NULL OR legacy_unassigned);
CREATE INDEX bot_sessions_project_created ON bot_sessions(project_id, created_at DESC, id DESC);

ALTER TABLE voice_enrollment_jobs
    ADD COLUMN project_id text REFERENCES projects(id),
    ADD COLUMN created_by_account_id text REFERENCES accounts(id);
CREATE INDEX voice_enrollment_jobs_project ON voice_enrollment_jobs(project_id, created_at DESC);

CREATE FUNCTION reject_new_legacy_unassigned() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' AND NEW.legacy_unassigned THEN
        RAISE check_violation USING MESSAGE = 'new rows require a project';
    END IF;
    IF TG_OP = 'UPDATE' AND NEW.legacy_unassigned AND NOT OLD.legacy_unassigned THEN
        RAISE check_violation USING MESSAGE = 'new rows require a project';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER meetings_reject_new_legacy BEFORE INSERT OR UPDATE ON meetings
    FOR EACH ROW EXECUTE FUNCTION reject_new_legacy_unassigned();
CREATE TRIGGER bot_sessions_reject_new_legacy BEFORE INSERT OR UPDATE ON bot_sessions
    FOR EACH ROW EXECUTE FUNCTION reject_new_legacy_unassigned();
