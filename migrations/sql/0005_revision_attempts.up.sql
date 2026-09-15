-- =====================================================================
-- C3 hardening: durable revision attempts.
-- Implements C3_HARDENING_DESIGN.md A.1-A.4, C.1 and H.2-H.6.
--
-- Schema only. Roles and grants live in 0005_revision_attempts.grants.up.sql
-- so that JOBFORGE_SKIP_ROLE_GRANTS=1 can skip privilege work on a managed
-- platform without also skipping the table, the triggers and the indexes -
-- which are not optional anywhere.
--
-- NOT CONCURRENTLY, deliberately (H.2). env.py wraps every revision in one
-- transaction and CREATE INDEX CONCURRENTLY cannot run inside one. The
-- attempts table is created empty in this same migration, so its indexes
-- block nothing. `uq_resume_version_per_application` is built on the existing
-- `resumes` table, which is small in every environment today.
--
--   RUNBOOK, large `resumes`: build it out of band first, from psql:
--     CREATE UNIQUE INDEX CONCURRENTLY uq_resume_version_per_application
--       ON resumes (application_id, kind, version)
--       WHERE application_id IS NOT NULL;
--   The IF NOT EXISTS below then becomes a no-op. This is documented rather
--   than automated: automating it means teaching env.py about
--   non-transactional revisions, a framework change this design does not need.
-- =====================================================================

-- ------------------------------------------------- 1. duplicate preflight
-- Aborts the whole migration if the backstop index cannot be built honestly.
-- No row is ever deleted, merged or renumbered by a migration (H.3).
DO $$
DECLARE dupes INTEGER;
BEGIN
  SELECT count(*) INTO dupes FROM (
    SELECT application_id, kind, version
      FROM resumes
     WHERE application_id IS NOT NULL
     GROUP BY 1,2,3
    HAVING count(*) > 1
  ) d;
  IF dupes > 0 THEN
    RAISE EXCEPTION
      'refusing to build uq_resume_version_per_application: % duplicate '
      '(application_id, kind, version) group(s) exist. Resolve them by hand; '
      'this migration will not delete or merge resume rows.', dupes;
  END IF;
END $$;

-- --------------------------------------------------------------- 2. types
-- `status` is about the JOB. `outcome` is about the CONTENT. A B5 negative
-- verdict is status='succeeded', outcome='failed_verification': the job did
-- exactly what it should, and v2 exists as a durable failed record. Keeping
-- those two axes apart is the single most important line in this design.
CREATE TYPE revision_attempt_status AS ENUM (
  'accepted','running','succeeded','failed','abandoned'
);

CREATE TYPE revision_outcome AS ENUM ('passed','failed_verification');

CREATE TYPE revision_failure_class AS ENUM (
  'model_timeout','model_output_invalid','budget_exceeded','evidence_stale',
  'evidence_locked','state_conflict','persistence_failed','lease_lost',
  'worker_lost'
);

-- --------------------------------------------------------------- 3. table
CREATE TABLE resume_revision_attempts (
  id                        UUID NOT NULL,
  application_id            UUID NOT NULL,
  source_resume_id          UUID NOT NULL,
  source_resume_version     INTEGER NOT NULL,
  source_approval_id        UUID NOT NULL,
  instructions_snapshot     TEXT NOT NULL,
  instructions_digest       TEXT NOT NULL,
  requested_by              UUID NOT NULL,
  requested_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
  status                    revision_attempt_status NOT NULL DEFAULT 'accepted',
  lease_owner               TEXT,
  lease_epoch               INTEGER NOT NULL DEFAULT 0,
  lease_expires_at          TIMESTAMPTZ,
  started_at                TIMESTAMPTZ,
  finished_at               TIMESTAMPTZ,
  result_resume_id          UUID,
  result_version            INTEGER,
  outcome                   revision_outcome,
  failure_class             revision_failure_class,
  failure_detail            TEXT,
  attempt_count             INTEGER NOT NULL DEFAULT 0,
  retry_exhausted           BOOLEAN NOT NULL DEFAULT false,
  evidence_stale_retry_count INTEGER NOT NULL DEFAULT 0,
  evidence_stale_retried_at TIMESTAMPTZ,
  agent_run_id              UUID,
  replaces_attempt_id       UUID,
  replaced_by_attempt_id    UUID,
  replacement_ordinal       INTEGER NOT NULL DEFAULT 0,
  abandoned_at              TIMESTAMPTZ,
  abandoned_by              UUID,
  abandon_reason            TEXT,
  recovered_at              TIMESTAMPTZ,
  created_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at                TIMESTAMPTZ NOT NULL DEFAULT now(),

  CONSTRAINT pk_resume_revision_attempts PRIMARY KEY (id),

  -- A.4. The source bindings are NO ACTION, not RESTRICT: RESTRICT fires per
  -- row and immediately, so deleting an application would cascade-delete the
  -- resume and approval and then abort on the attempt's own references - even
  -- though the attempt is being cascade-deleted in the same statement. NO
  -- ACTION is checked at end of statement, by which time the attempt is gone.
  -- Provenance is still undeletable in every ordinary path.
  CONSTRAINT fk_resume_revision_attempts_application_id_applications
    FOREIGN KEY (application_id) REFERENCES applications (id) ON DELETE CASCADE,
  CONSTRAINT fk_resume_revision_attempts_source_resume_id_resumes
    FOREIGN KEY (source_resume_id) REFERENCES resumes (id) ON DELETE NO ACTION,
  CONSTRAINT fk_resume_revision_attempts_source_approval_id_approvals
    FOREIGN KEY (source_approval_id) REFERENCES approvals (id) ON DELETE NO ACTION,
  -- NOT SET NULL: that would contradict ck_resume_revision_attempts_succeeded_requires_result,
  -- which would then fail on a row nobody touched.
  CONSTRAINT fk_resume_revision_attempts_result_resume_id_resumes
    FOREIGN KEY (result_resume_id) REFERENCES resumes (id) ON DELETE NO ACTION,
  CONSTRAINT fk_resume_revision_attempts_requested_by_users
    FOREIGN KEY (requested_by) REFERENCES users (id) ON DELETE RESTRICT,
  CONSTRAINT fk_resume_revision_attempts_abandoned_by_users
    FOREIGN KEY (abandoned_by) REFERENCES users (id) ON DELETE RESTRICT,
  CONSTRAINT fk_resume_revision_attempts_agent_run_id_agent_runs
    FOREIGN KEY (agent_run_id) REFERENCES agent_runs (id) ON DELETE SET NULL,
  CONSTRAINT fk_resume_revision_attempts_replaces_attempt_id_self
    FOREIGN KEY (replaces_attempt_id) REFERENCES resume_revision_attempts (id)
    ON DELETE NO ACTION,
  CONSTRAINT fk_resume_revision_attempts_replaced_by_attempt_id_self
    FOREIGN KEY (replaced_by_attempt_id) REFERENCES resume_revision_attempts (id)
    ON DELETE NO ACTION,

  -- A.3. Every invariant below holds at every value of every setting. No
  -- tunable number appears here: raising a limit must not need a migration.
  CONSTRAINT ck_resume_revision_attempts_succeeded_requires_result
    CHECK (status <> 'succeeded' OR (result_resume_id IS NOT NULL
           AND result_version IS NOT NULL AND outcome IS NOT NULL
           AND finished_at IS NOT NULL)),
  CONSTRAINT ck_resume_revision_attempts_failed_requires_class
    CHECK (status <> 'failed' OR failure_class IS NOT NULL),
  CONSTRAINT ck_resume_revision_attempts_open_has_no_result
    CHECK (status NOT IN ('accepted','running')
           OR (result_resume_id IS NULL AND result_version IS NULL
               AND outcome IS NULL)),
  -- This is what makes "a semantic negative can never wear a failed status"
  -- a database fact rather than a convention.
  CONSTRAINT ck_resume_revision_attempts_outcome_only_on_success
    CHECK (outcome IS NULL OR status = 'succeeded'),
  CONSTRAINT ck_resume_revision_attempts_abandoned_named_a_human
    CHECK (status <> 'abandoned'
           OR (abandoned_at IS NOT NULL AND abandoned_by IS NOT NULL)),
  CONSTRAINT ck_resume_revision_attempts_running_has_lease
    CHECK (status <> 'running'
           OR (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)),
  CONSTRAINT ck_resume_revision_attempts_replacement_ordinal_non_negative
    CHECK (replacement_ordinal >= 0),
  -- Deliberately encodes NO ceiling. The evidence-stale budget is a property
  -- of the shared retry predicate (C.1.1); this states only that the counter
  -- is non-negative and that the counter and its timestamp agree.
  CONSTRAINT ck_resume_revision_attempts_evidence_stale_retry_coherent
    CHECK (evidence_stale_retry_count >= 0
           AND (evidence_stale_retry_count = 0) = (evidence_stale_retried_at IS NULL))
);

-- ------------------------------------------------------------- 4. indexes
-- THE rule: one source changes-requested approval => at most one LIVE attempt.
-- Same partial-unique pattern as the existing uq_one_pending_approval.
CREATE UNIQUE INDEX uq_revision_attempt_per_approval
  ON resume_revision_attempts (source_approval_id)
  WHERE status <> 'abandoned';

CREATE INDEX idx_revision_attempts_claimable
  ON resume_revision_attempts (lease_expires_at)
  WHERE status IN ('accepted', 'running');

CREATE INDEX ix_revision_attempts_application_id
  ON resume_revision_attempts (application_id);

-- --------------------------------------- 5. version allocation backstop
-- Blocker 6. IF NOT EXISTS so the CONCURRENTLY runbook above is a no-op here.
CREATE UNIQUE INDEX IF NOT EXISTS uq_resume_version_per_application
  ON resumes (application_id, kind, version)
  WHERE application_id IS NOT NULL;

-- ------------------------------------------------ 6. immutability trigger
-- Source bindings never move, retry history only moves forward, and a
-- terminal row is frozen. Whole-row comparison for the frozen case, the same
-- shape as 0004's events guard, so a column added by a later migration is
-- protected automatically rather than by an enumeration that drifts.
CREATE OR REPLACE FUNCTION jobforge_revision_attempt_source_immutable()
RETURNS TRIGGER AS $$
DECLARE
  old_frozen JSONB;
  new_frozen JSONB;
  changed    TEXT;
BEGIN
  IF NEW.application_id        IS DISTINCT FROM OLD.application_id
     OR NEW.source_resume_id      IS DISTINCT FROM OLD.source_resume_id
     OR NEW.source_resume_version IS DISTINCT FROM OLD.source_resume_version
     OR NEW.source_approval_id    IS DISTINCT FROM OLD.source_approval_id
     OR NEW.instructions_snapshot IS DISTINCT FROM OLD.instructions_snapshot
     OR NEW.instructions_digest   IS DISTINCT FROM OLD.instructions_digest
     OR NEW.requested_by          IS DISTINCT FROM OLD.requested_by
     OR NEW.replaces_attempt_id   IS DISTINCT FROM OLD.replaces_attempt_id
     OR NEW.replacement_ordinal   IS DISTINCT FROM OLD.replacement_ordinal
  THEN
    RAISE EXCEPTION
      'revision attempt source bindings are immutable (application, source '
      'resume/version/approval, instructions, requester, replacement lineage)'
      USING ERRCODE = 'raise_exception';
  END IF;

  -- Retry history is append-only in the same sense the bindings are
  -- immutable. `failure_class` may be rewritten on each report - that is how
  -- a retryable class becomes a terminal one - but the columns recording how
  -- much budget has been spent can only move forward. Without this, an
  -- attempt that consumed its evidence_stale retry and later failed with
  -- model_timeout could be re-reported as evidence_stale and silently earn a
  -- second one.
  IF NEW.evidence_stale_retry_count < OLD.evidence_stale_retry_count THEN
    RAISE EXCEPTION
      'evidence_stale_retry_count is monotonic: % -> % is not permitted',
      OLD.evidence_stale_retry_count, NEW.evidence_stale_retry_count
      USING ERRCODE = 'raise_exception';
  END IF;
  IF NEW.attempt_count < OLD.attempt_count THEN
    RAISE EXCEPTION 'attempt_count is monotonic: % -> % is not permitted',
      OLD.attempt_count, NEW.attempt_count
      USING ERRCODE = 'raise_exception';
  END IF;
  IF OLD.evidence_stale_retried_at IS NOT NULL
     AND NEW.evidence_stale_retried_at IS NULL THEN
    RAISE EXCEPTION 'evidence_stale_retried_at is never cleared'
      USING ERRCODE = 'raise_exception';
  END IF;

  -- Frozen once terminal. A succeeded row is immutable in every column, so
  -- every UPDATE against one is refused - the updated_at trigger fires first
  -- and is itself a change, which is the intended behaviour.
  IF OLD.status = 'succeeded' THEN
    IF to_jsonb(NEW) IS DISTINCT FROM to_jsonb(OLD) THEN
      SELECT string_agg(key, ', ' ORDER BY key) INTO changed
      FROM jsonb_each(to_jsonb(OLD)) AS o(key, value)
      WHERE o.value IS DISTINCT FROM (to_jsonb(NEW) -> o.key);
      RAISE EXCEPTION
        'a succeeded revision attempt is immutable; attempted to modify: %',
        COALESCE(changed, '(column set changed)')
        USING ERRCODE = 'raise_exception';
    END IF;
  ELSIF OLD.status = 'abandoned' THEN
    -- One exception in the whole design: replaced_by_attempt_id may be set
    -- once, from NULL, so the replacement chain can be closed after the fact.
    old_frozen := to_jsonb(OLD) - 'updated_at' - 'replaced_by_attempt_id';
    new_frozen := to_jsonb(NEW) - 'updated_at' - 'replaced_by_attempt_id';
    IF new_frozen IS DISTINCT FROM old_frozen THEN
      SELECT string_agg(key, ', ' ORDER BY key) INTO changed
      FROM jsonb_each(old_frozen) AS o(key, value)
      WHERE o.value IS DISTINCT FROM (new_frozen -> o.key);
      RAISE EXCEPTION
        'an abandoned revision attempt is immutable except for '
        'replaced_by_attempt_id; attempted to modify: %',
        COALESCE(changed, '(column set changed)')
        USING ERRCODE = 'raise_exception';
    END IF;
    IF OLD.replaced_by_attempt_id IS NOT NULL
       AND NEW.replaced_by_attempt_id IS DISTINCT FROM OLD.replaced_by_attempt_id
    THEN
      RAISE EXCEPTION 'replaced_by_attempt_id is set once and never changed'
        USING ERRCODE = 'raise_exception';
    END IF;
  END IF;

  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- -------------------------------------------------- 7. transition trigger
-- C.1 is the authority. A status change absent from it is refused here, the
-- same way ALLOWED_TRANSITIONS guards the application machine. running ->
-- running is permitted because it carries both takeover (row 3) and renewal
-- (row 4); which of the two it is, is decided by the statement predicates in
-- D.2/D.5, not by this trigger.
CREATE OR REPLACE FUNCTION jobforge_revision_attempt_transition()
RETURNS TRIGGER AS $$
BEGIN
  IF NEW.status = OLD.status THEN
    RETURN NEW;
  END IF;

  IF NOT (
       (OLD.status = 'accepted' AND NEW.status IN ('running','failed','abandoned'))
    OR (OLD.status = 'running'  AND NEW.status IN ('succeeded','accepted','failed'))
    OR (OLD.status = 'failed'   AND NEW.status = 'abandoned')
  ) THEN
    RAISE EXCEPTION
      'revision attempt transition % -> % is not permitted (C.1)',
      OLD.status, NEW.status
      USING ERRCODE = 'raise_exception';
  END IF;

  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Trigger names fix the firing order, which is alphabetical per event:
--   set_updated_at  -> source_immutable -> transition
-- so updated_at is stamped before the guards inspect the row.
CREATE TRIGGER trg_resume_revision_attempts_set_updated_at
  BEFORE UPDATE ON resume_revision_attempts
  FOR EACH ROW EXECUTE FUNCTION jobforge_set_updated_at();

CREATE TRIGGER trg_resume_revision_attempts_source_immutable
  BEFORE UPDATE ON resume_revision_attempts
  FOR EACH ROW EXECUTE FUNCTION jobforge_revision_attempt_source_immutable();

CREATE TRIGGER trg_resume_revision_attempts_transition
  BEFORE UPDATE ON resume_revision_attempts
  FOR EACH ROW EXECUTE FUNCTION jobforge_revision_attempt_transition();
