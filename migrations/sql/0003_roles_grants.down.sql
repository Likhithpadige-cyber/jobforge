-- Reverse of 0003_roles_grants.up.sql.
--
-- Grants are revoked; ROLES ARE NOT DROPPED. Roles are cluster-level and may
-- own objects in other databases, so dropping them from a per-database
-- migration is unsafe. Remove them deliberately with `make db-roles-drop`.

ALTER DEFAULT PRIVILEGES IN SCHEMA public
  REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM app_api;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  REVOKE SELECT ON TABLES FROM app_agent, app_monitor;

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_api') THEN
    REVOKE ALL ON ALL TABLES IN SCHEMA public FROM app_api;
    REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM app_api;
    REVOKE ALL ON SCHEMA public FROM app_api;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_agent') THEN
    REVOKE ALL ON ALL TABLES IN SCHEMA public FROM app_agent;
    REVOKE ALL ON SCHEMA public FROM app_agent;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_monitor') THEN
    REVOKE ALL ON ALL TABLES IN SCHEMA public FROM app_monitor;
    REVOKE ALL ON SCHEMA public FROM app_monitor;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_migrate') THEN
    REVOKE ALL ON SCHEMA public FROM app_migrate;
  END IF;
END $$;
