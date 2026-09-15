-- =====================================================================
-- C3 hardening: privileges for 0005.
-- Implements C3_HARDENING_DESIGN.md H.4 and I.6.1-I.6.2.
--
-- Split from the schema file so JOBFORGE_SKIP_ROLE_GRANTS=1 can skip this
-- and this alone. On a managed platform the roles are provisioned out of
-- band; the table, triggers and indexes are never optional.
--
-- Two repo facts this file must account for. 0003 sets
--   ALTER DEFAULT PRIVILEGES IN SCHEMA public
--     GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app_api
--   ALTER DEFAULT PRIVILEGES IN SCHEMA public
--     GRANT SELECT ON TABLES TO app_agent, app_monitor
-- so resume_revision_attempts ARRIVES with app_api holding DELETE and with
-- app_agent holding table-wide SELECT. The REVOKEs below are load-bearing,
-- not decorative, and the grants are restated so a reader of 0005 sees the
-- whole privilege picture in one place.
-- =====================================================================

-- ------------------------------------------------------ 1. app_audit role
-- New in 0005. The inference-audit role: may write the four audit tables,
-- and nothing else, ever. NOINHERIT matches the other four roles - it must
-- not pick up privileges through a membership, now or after a future edit.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_audit') THEN
    CREATE ROLE app_audit LOGIN NOINHERIT;
  END IF;
END $$;

-- Stated rather than assumed, so a later ALTER ROLE is a visible diff.
ALTER ROLE app_audit NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;

-- Default-deny prologue. 0003 sets ALTER DEFAULT PRIVILEGES for app_api /
-- app_agent / app_monitor only, so app_audit inherits nothing by default;
-- these REVOKEs are belt-and-braces against PUBLIC grants and future edits.
REVOKE ALL ON ALL TABLES    IN SCHEMA public FROM app_audit;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM app_audit;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM app_audit;
REVOKE ALL ON SCHEMA public FROM app_audit;
GRANT USAGE ON SCHEMA public TO app_audit;   -- resolve names; create nothing

-- ------------------------------------------ 2. the audit matrix (I.6.1)
-- Column lists are exactly the attributes the B stages assign to a run, step
-- or call object AFTER session.add() + session.flush(). Nothing wider.
GRANT SELECT, INSERT ON agent_runs TO app_audit;
GRANT UPDATE (
  status, steps_completed, input_tokens, output_tokens, cost_usd,
  prompt_version, error_type, error_message, duration_ms, finished_at
) ON agent_runs TO app_audit;

GRANT SELECT, INSERT ON agent_run_steps TO app_audit;
GRANT UPDATE (
  status, input_digest, output, guardrail_results, duration_ms, finished_at
) ON agent_run_steps TO app_audit;

-- `call.response_redacted = canonical(safe_output)` runs after the row is
-- inserted and flushed, in all four stages. This column and no other.
GRANT SELECT, INSERT ON llm_calls TO app_audit;
GRANT UPDATE (response_redacted) ON llm_calls TO app_audit;

-- No UPDATE at all: publishing is the outbox relay's job, and the relay runs
-- as app_api/app_agent, which already hold UPDATE (published, published_at).
GRANT SELECT, INSERT ON events TO app_audit;

-- No sequence grant. events.id, agent_runs.id, agent_run_steps.id and
-- llm_calls.id are UUID primary keys supplied by the application (uuid7);
-- 0002 declares no SERIAL, BIGSERIAL or nextval() default anywhere. A
-- standing schema-wide privilege bought for nothing is still a privilege.

-- Explicit denials, so a reader of 0005 sees them stated rather than implied.
REVOKE DELETE, TRUNCATE, REFERENCES, TRIGGER ON
  agent_runs, agent_run_steps, llm_calls, events FROM app_audit;
REVOKE ALL ON applications, resumes, resume_claims, approvals,
              profile_facts, profiles, application_status_history,
              audit_logs, settings, users
  FROM app_audit;

-- The one directly-callable SECURITY DEFINER function that mutates a domain
-- table (it does UPDATE profile_facts SET usage_count = usage_count + 1).
-- 0002 revokes it from PUBLIC and 0003 grants it to app_agent, app_api only;
-- restated because "already revoked" is exactly the kind of fact a later
-- migration undoes by accident.
REVOKE EXECUTE ON FUNCTION jobforge_increment_fact_usage(UUID) FROM app_audit, PUBLIC;

-- ------------------------------------- 3. resume_revision_attempts (H.4)
-- app_api: the human request path creates and reads attempts.
GRANT SELECT, INSERT, UPDATE ON resume_revision_attempts TO app_api;
REVOKE DELETE ON resume_revision_attempts FROM app_api;   -- audit row

-- app_agent: the worker. No INSERT: an attempt is created by a human
-- request, the same principle that keeps app_agent off `approvals`.
GRANT SELECT ON resume_revision_attempts TO app_agent;
GRANT UPDATE (
  status, lease_owner, lease_epoch, lease_expires_at,
  started_at, finished_at, result_resume_id, result_version,
  outcome, failure_class, failure_detail, attempt_count,
  retry_exhausted, agent_run_id, updated_at,
  evidence_stale_retry_count, evidence_stale_retried_at
) ON resume_revision_attempts TO app_agent;
REVOKE INSERT, DELETE ON resume_revision_attempts FROM app_agent;

-- Column-level UPDATE means a worker physically cannot write abandoned_by,
-- abandon_reason, replaces_attempt_id, recovered_at or any source binding -
-- the same defence-in-depth shape `applications` already uses for its
-- approval columns. The two retry columns are writable because D.6 is a
-- worker statement, but they can only ever move FORWARD: the monotonicity
-- clause in jobforge_revision_attempt_source_immutable refuses a decrement
-- regardless of the role issuing it. Column grants say WHICH columns; the
-- trigger says WHICH DIRECTION.

-- app_monitor stays read-only.
GRANT SELECT ON resume_revision_attempts TO app_monitor;
REVOKE INSERT, UPDATE, DELETE ON resume_revision_attempts FROM app_monitor;

-- app_audit: no privilege of any kind here. It is not an audit table.
REVOKE ALL ON resume_revision_attempts FROM app_audit;

-- Nobody deletes a revision attempt. It is the evidence that the attempt
-- happened, which is the whole point of the durable-attempt model (PO-4).
REVOKE DELETE ON resume_revision_attempts FROM PUBLIC;
