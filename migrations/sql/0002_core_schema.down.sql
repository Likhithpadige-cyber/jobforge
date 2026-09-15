-- =====================================================================
-- Phase 2 core schema, reversed. Generated from 0002_core_schema.up.sql
-- by scripts/gen_down_migration.py so it cannot drift out of step.
--
-- Tables drop in reverse creation order with CASCADE, which also removes
-- the triggers and indexes attached to them. Extensions are NOT dropped:
-- other schemas in the same database may depend on them.
-- =====================================================================

-- 1. drop the deferred FKs that close the applications <-> resumes cycle
ALTER TABLE IF EXISTS applications
  DROP CONSTRAINT IF EXISTS fk_applications_resume_id_resumes;
ALTER TABLE IF EXISTS applications
  DROP CONSTRAINT IF EXISTS fk_applications_cover_letter_id_cover_letters;

-- 2. tables, reverse dependency order

DROP TABLE IF EXISTS settings CASCADE;
DROP TABLE IF EXISTS audit_logs CASCADE;
DROP TABLE IF EXISTS notifications CASCADE;
DROP TABLE IF EXISTS alerts CASCADE;
DROP TABLE IF EXISTS metrics_snapshots CASCADE;
DROP TABLE IF EXISTS daily_quotas CASCADE;
DROP TABLE IF EXISTS fact_evidence CASCADE;
DROP TABLE IF EXISTS application_status_history CASCADE;
DROP TABLE IF EXISTS submissions CASCADE;
DROP TABLE IF EXISTS artifacts CASCADE;
DROP TABLE IF EXISTS approvals CASCADE;
DROP TABLE IF EXISTS application_answers CASCADE;
DROP TABLE IF EXISTS application_questions CASCADE;
DROP TABLE IF EXISTS cover_letter_claims CASCADE;
DROP TABLE IF EXISTS cover_letters CASCADE;
DROP TABLE IF EXISTS resume_claims CASCADE;
DROP TABLE IF EXISTS resumes CASCADE;
DROP TABLE IF EXISTS applications CASCADE;
DROP TABLE IF EXISTS job_evaluations CASCADE;
DROP TABLE IF EXISTS llm_calls CASCADE;
DROP TABLE IF EXISTS agent_run_steps CASCADE;
DROP TABLE IF EXISTS agent_runs CASCADE;
DROP TABLE IF EXISTS processed_events CASCADE;
DROP TABLE IF EXISTS events CASCADE;
DROP TABLE IF EXISTS job_raw_snapshots CASCADE;
DROP TABLE IF EXISTS jobs CASCADE;
DROP TABLE IF EXISTS sources CASCADE;
DROP TABLE IF EXISTS site_policies CASCADE;
DROP TABLE IF EXISTS companies CASCADE;
DROP TABLE IF EXISTS profile_facts CASCADE;
DROP TABLE IF EXISTS projects CASCADE;
DROP TABLE IF EXISTS skills CASCADE;
DROP TABLE IF EXISTS certifications CASCADE;
DROP TABLE IF EXISTS educations CASCADE;
DROP TABLE IF EXISTS experiences CASCADE;
DROP TABLE IF EXISTS profiles CASCADE;
DROP TABLE IF EXISTS users CASCADE;

-- 3. functions
DROP FUNCTION IF EXISTS jobforge_block_mutation() CASCADE;
DROP FUNCTION IF EXISTS jobforge_check_claim_facts() CASCADE;
DROP FUNCTION IF EXISTS jobforge_events_publish_only() CASCADE;
DROP FUNCTION IF EXISTS jobforge_increment_fact_usage(UUID) CASCADE;
DROP FUNCTION IF EXISTS jobforge_protect_verified_facts() CASCADE;
DROP FUNCTION IF EXISTS jobforge_require_real_approval() CASCADE;
DROP FUNCTION IF EXISTS jobforge_set_updated_at() CASCADE;
DROP FUNCTION IF EXISTS jobforge_write_audit() CASCADE;
DROP FUNCTION IF EXISTS jobforge_write_audit_settings() CASCADE;

-- 4. enum types
DROP TYPE IF EXISTS alert_severity;
DROP TYPE IF EXISTS run_status;
DROP TYPE IF EXISTS agent_name;
DROP TYPE IF EXISTS recommendation;
DROP TYPE IF EXISTS ingestion_method;
DROP TYPE IF EXISTS submission_result;
DROP TYPE IF EXISTS submission_method;
DROP TYPE IF EXISTS answer_source;
DROP TYPE IF EXISTS question_class;
DROP TYPE IF EXISTS claim_verdict;
DROP TYPE IF EXISTS fact_status;
DROP TYPE IF EXISTS fact_type;
DROP TYPE IF EXISTS approval_kind;
DROP TYPE IF EXISTS approval_status;
DROP TYPE IF EXISTS application_outcome;
DROP TYPE IF EXISTS application_status;
DROP TYPE IF EXISTS job_status;

-- 5. extensions are deliberately left in place (see header).
