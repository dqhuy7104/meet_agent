ALTER TABLE voice_enrollment_jobs
    DROP CONSTRAINT voice_enrollment_jobs_profile_id_fkey,
    DROP CONSTRAINT voice_enrollment_jobs_source_meeting_id_fkey,
    DROP CONSTRAINT voice_enrollment_jobs_check,
    ALTER COLUMN profile_id DROP NOT NULL,
    ADD FOREIGN KEY (profile_id) REFERENCES voice_profiles(id) ON DELETE SET NULL,
    ADD FOREIGN KEY (source_meeting_id) REFERENCES meetings(id) ON DELETE SET NULL,
    ADD COLUMN pipeline_pid integer,
    ADD COLUMN pipeline_start_ticks bigint,
    ADD CHECK ((source = 'upload' AND upload_key IS NOT NULL AND source_meeting_id IS NULL AND source_speaker IS NULL)
        OR (source = 'meeting' AND upload_key IS NULL AND source_speaker IS NOT NULL));

CREATE FUNCTION fail_deleted_voice_source() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.profile_id IS NULL OR (NEW.source = 'meeting' AND NEW.source_meeting_id IS NULL) THEN
        IF NEW.state IN ('queued', 'processing') THEN
            NEW.state := 'failed';
            NEW.failure_code := 'source_changed';
            NEW.updated_at := now();
        END IF;
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER voice_enrollment_source_deleted BEFORE UPDATE ON voice_enrollment_jobs
    FOR EACH ROW EXECUTE FUNCTION fail_deleted_voice_source();

CREATE FUNCTION cancel_meeting_voice_data() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.status = 'cancelled' AND OLD.status <> 'cancelled' THEN
        DELETE FROM voice_samples WHERE source_meeting_id = NEW.id;
        DELETE FROM meeting_speaker_identities WHERE meeting_id = NEW.id;
        UPDATE voice_enrollment_jobs SET state = 'failed', failure_code = 'source_changed', updated_at = now()
            WHERE source_meeting_id = NEW.id AND state IN ('queued', 'processing');
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER meeting_voice_cancelled AFTER UPDATE OF status ON meetings
    FOR EACH ROW EXECUTE FUNCTION cancel_meeting_voice_data();
