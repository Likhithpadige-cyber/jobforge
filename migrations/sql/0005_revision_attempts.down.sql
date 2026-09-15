-- =====================================================================
-- Reverse 0005's schema. This is for a failed deploy, not a considered
-- rollback: revision attempts are audit history and dropping the table
-- destroys it (H.7).
-- =====================================================================

DO $$
BEGIN
  IF current_setting('jobforge.allow_destructive_revision_rollback', true) IS DISTINCT FROM '1'
     AND EXISTS (SELECT 1 FROM resume_revision_attempts) THEN
    RAISE EXCEPTION
      'refusing to drop resume_revision_attempts: % row(s) of revision audit '
      'history exist. This down migration is for a failed deploy, not a '
      'considered rollback. Set '
      'JOBFORGE_ALLOW_DESTRUCTIVE_REVISION_ROLLBACK=1 if you have exported '
      'the audit trail.',
      (SELECT count(*) FROM resume_revision_attempts);
  END IF;
END $$;

DROP TRIGGER IF EXISTS trg_resume_revision_attempts_transition
  ON resume_revision_attempts;
DROP TRIGGER IF EXISTS trg_resume_revision_attempts_source_immutable
  ON resume_revision_attempts;
DROP TRIGGER IF EXISTS trg_resume_revision_attempts_set_updated_at
  ON resume_revision_attempts;

DROP FUNCTION IF EXISTS jobforge_revision_attempt_transition();
DROP FUNCTION IF EXISTS jobforge_revision_attempt_source_immutable();

-- Dropped too, and the header says so: leaving a constraint behind after a
-- rollback is its own drift.
DROP INDEX IF EXISTS uq_resume_version_per_application;

DROP INDEX IF EXISTS ix_revision_attempts_application_id;
DROP INDEX IF EXISTS idx_revision_attempts_claimable;
DROP INDEX IF EXISTS uq_revision_attempt_per_approval;

DROP TABLE IF EXISTS resume_revision_attempts CASCADE;

DROP TYPE IF EXISTS revision_failure_class;
DROP TYPE IF EXISTS revision_outcome;
DROP TYPE IF EXISTS revision_attempt_status;
