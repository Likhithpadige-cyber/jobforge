-- =====================================================================
-- Database roles and grants. Implements DATABASE_DESIGN.md §8 and the
-- second authorization layer described in SECURITY.md §3.2.
--
-- The point of this file: an agent worker connecting as app_agent must be
-- PHYSICALLY unable to insert a profile fact, approve an application, or
-- alter a site policy, regardless of what its code or its model output
-- tries to do. Application-layer checks are the first line; these grants
-- are the line that still holds when the first one has a bug.
--
-- Roles are cluster-level objects. CREATE ROLE is guarded so re-running is
-- safe, and passwords are NEVER set here - they come from Secrets Manager
-- (or .env locally) and are applied out of band by `make db-roles`.
-- =====================================================================

-- --------------------------------------------------------------- 1. roles
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_api') THEN
    CREATE ROLE app_api LOGIN NOINHERIT;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_agent') THEN
    CREATE ROLE app_agent LOGIN NOINHERIT;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_monitor') THEN
    CREATE ROLE app_monitor LOGIN NOINHERIT;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_migrate') THEN
    CREATE ROLE app_migrate LOGIN NOINHERIT;
  END IF;
END $$;

-- No role may create objects in public, so an agent cannot define a table,
-- a function, or a view to work around a grant.
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO app_api, app_agent, app_monitor;
GRANT USAGE, CREATE ON SCHEMA public TO app_migrate;

-- ----------------------------------------------------------- 2. app_api
-- The human-facing API. Full CRUD on operational tables, including the
-- profile and fact tables, because a human acting through the UI is the
-- only legitimate author of a fact.
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO app_api;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO app_api;

-- Even app_api may not rewrite history.
REVOKE UPDATE, DELETE ON events, audit_logs, application_status_history,
  job_evaluations FROM app_api;
GRANT UPDATE (published, published_at) ON events TO app_api;

-- --------------------------------------------------------- 3. app_agent
-- Read broadly, write narrowly.
GRANT SELECT ON ALL TABLES IN SCHEMA public TO app_agent;

GRANT INSERT, UPDATE ON
  jobs, job_evaluations, resumes, resume_claims, cover_letters,
  cover_letter_claims, application_questions, application_answers,
  submissions, agent_runs, agent_run_steps, llm_calls, processed_events
  TO app_agent;

GRANT INSERT ON events, application_status_history TO app_agent;
GRANT UPDATE (published, published_at) ON events TO app_agent;

-- job_evaluations is append-only; INSERT above is what agents need, and the
-- append-only trigger blocks the UPDATE half regardless. Remove it anyway so
-- the grant matches the intent.
REVOKE UPDATE ON job_evaluations FROM app_agent;

-- applications: agents drive the pipeline but must never manufacture an
-- approval. Column-level grants exclude the approval columns entirely.
-- Combined with ck_applications_approved_requires_human and the
-- trg_applications_require_real_approval trigger, there is no path by which
-- an agent moves an application into an approved state.
GRANT INSERT ON applications TO app_agent;
GRANT UPDATE (
  status, outcome, resume_id, cover_letter_id, submission_method, priority,
  submitted_at, outcome_updated_at, error_reason, abandon_reason, notes,
  version, updated_at
) ON applications TO app_agent;

-- The explicit denials. Most are already absent, but stating them means a
-- future `GRANT ... ON ALL TABLES` cannot silently widen the agent role.
REVOKE INSERT, UPDATE, DELETE ON
  profile_facts, profiles, experiences, educations, certifications, skills,
  projects, fact_evidence, site_policies, sources, settings, users, approvals,
  companies, alerts, notifications, metrics_snapshots, audit_logs,
  daily_quotas, artifacts
  FROM app_agent;

-- Agents record fact usage through a narrow definer-rights function rather
-- than holding UPDATE on profile_facts (DATABASE_DESIGN.md §4.4).
GRANT EXECUTE ON FUNCTION jobforge_increment_fact_usage(UUID) TO app_agent, app_api;

-- ------------------------------------------------------- 4. app_monitor
-- Agent 3 is read-mostly and cannot influence any application's fate.
GRANT SELECT ON ALL TABLES IN SCHEMA public TO app_monitor;
GRANT INSERT, UPDATE ON metrics_snapshots, alerts, notifications,
  processed_events TO app_monitor;
GRANT INSERT ON events TO app_monitor;
REVOKE INSERT, UPDATE, DELETE ON
  profile_facts, profiles, experiences, educations, certifications, skills,
  projects, fact_evidence, applications, approvals, resumes, resume_claims,
  cover_letters, cover_letter_claims, submissions, jobs, job_evaluations,
  site_policies, sources, settings, users, application_answers,
  application_questions, artifacts, audit_logs
  FROM app_monitor;

-- --------------------------------------------------------- 5. universal
-- Nobody deletes the audit trail. Not app_api, not a superuser acting
-- through the application, not a migration.
REVOKE DELETE ON events, audit_logs, application_status_history FROM PUBLIC;
REVOKE DELETE ON events, audit_logs, application_status_history
  FROM app_api, app_agent, app_monitor;

-- Agents must not be able to widen their own access. NOINHERIT above stops
-- privilege inheritance; this stops them handing grants to anyone else.
-- (WITH GRANT OPTION is never used in this file, so no role can re-grant.)

-- ---------------------------------------------------- 6. future objects
-- Default privileges so a table added by a later migration does not
-- silently arrive with no grants - or worse, with the wrong ones.
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app_api;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT ON TABLES TO app_agent, app_monitor;
