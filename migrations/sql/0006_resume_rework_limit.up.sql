-- Each terminal resume approval records one human request-changes cycle.
-- Historical decisions are counted in place; no counter or data rewrite.
CREATE INDEX ix_approvals_resume_rework_cycles
  ON approvals (application_id)
  WHERE kind = 'resume' AND status = 'changes_requested';

CREATE OR REPLACE FUNCTION jobforge_guard_resume_rework_cycles() RETURNS TRIGGER AS $$
DECLARE
  prior_cycles INTEGER;
BEGIN
  IF TG_OP = 'DELETE' THEN
    IF OLD.kind = 'resume' AND OLD.status = 'changes_requested' THEN
      RAISE EXCEPTION 'a counted resume rework decision cannot be deleted'
        USING ERRCODE = 'raise_exception';
    END IF;
    RETURN OLD;
  END IF;

  IF TG_OP = 'INSERT' THEN
    IF NEW.kind = 'resume' AND NEW.status = 'changes_requested' THEN
      RAISE EXCEPTION 'a resume rework decision must transition from pending'
        USING ERRCODE = 'raise_exception';
    END IF;
    RETURN NEW;
  END IF;

  IF OLD.kind = 'resume' AND OLD.status = 'changes_requested' THEN
    IF NEW IS DISTINCT FROM OLD THEN
      RAISE EXCEPTION 'a counted resume rework decision cannot be rewritten'
        USING ERRCODE = 'raise_exception';
    END IF;
    RETURN NEW;
  END IF;

  IF NEW.kind = 'resume' AND NEW.status = 'changes_requested' THEN
    IF OLD.kind <> 'resume' OR OLD.status <> 'pending'
       OR NEW.application_id IS DISTINCT FROM OLD.application_id
       OR NEW.decided_by IS NULL OR NEW.decided_at IS NULL THEN
      RAISE EXCEPTION 'a resume rework decision requires a pending resume approval and human actor'
        USING ERRCODE = 'raise_exception';
    END IF;

    -- The application row is already locked by the supported service path.
    -- This transaction-scoped lock also serializes direct writers that skip it.
    -- A hash collision only serializes unrelated applications; it cannot weaken
    -- the limit. The count query runs after the lock is acquired.
    PERFORM pg_advisory_xact_lock(
      hashtextextended('resume-rework:' || NEW.application_id::text, 0)
    );
    SELECT count(*) INTO prior_cycles
    FROM approvals
    WHERE application_id = NEW.application_id
      AND kind = 'resume'
      AND status = 'changes_requested';
    IF prior_cycles >= 3 THEN
      RAISE EXCEPTION 'resume rework limit reached for application %', NEW.application_id
        USING ERRCODE = 'raise_exception';
    END IF;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_approvals_resume_rework_cycles
  BEFORE INSERT OR UPDATE OR DELETE ON approvals
  FOR EACH ROW EXECUTE FUNCTION jobforge_guard_resume_rework_cycles();
