-- Reverse 0005's privileges. Revoke before dropping: DROP ROLE fails while
-- the role still holds a privilege on any object, and that failure is the
-- point - it stops a half-removed role lingering with rights nobody expects.
REVOKE ALL ON resume_revision_attempts FROM app_api, app_agent, app_monitor;
REVOKE ALL ON agent_runs, agent_run_steps, llm_calls, events FROM app_audit;
REVOKE ALL ON SCHEMA public FROM app_audit;

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_audit') THEN
    -- Refuses if the role still owns an object, deliberately.
    DROP ROLE app_audit;
  END IF;
END $$;
