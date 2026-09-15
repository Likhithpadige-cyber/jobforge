-- =====================================================================
-- Phase 2 core schema. Implements DATABASE_DESIGN.md §3-§7.
--
-- Order is significant and explicit:
--   1. extensions
--   2. enum types (before any table that uses them)
--   3. tables, in dependency order
--   4. deferred FKs for the applications <-> resumes cycle
--   5. indexes
--   6. triggers enforcing the invariants a CHECK cannot express
--
-- Constraint names follow the SQLAlchemy naming convention declared in
-- packages/core/db/base.py, so Alembic autogenerate stays quiet.
-- =====================================================================

-- ------------------------------------------------------------ 1. extensions
CREATE EXTENSION IF NOT EXISTS "pgcrypto";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";
CREATE EXTENSION IF NOT EXISTS "btree_gin";
CREATE EXTENSION IF NOT EXISTS "vector";
-- citext: users.email is case-insensitive. Not in the 0001 baseline.
CREATE EXTENSION IF NOT EXISTS "citext";

-- ----------------------------------------------------------------- 2. enums
CREATE TYPE job_status AS ENUM (
  'discovered','normalized','evaluated','shortlisted','queued',
  'claimed','applied','discarded','duplicate','expired');

CREATE TYPE application_status AS ENUM (
  'created','resume_generating','resume_ready','verification_failed',
  'questions_pending','awaiting_approval','changes_requested','approved',
  'submission_ready','submitting','submitted','submission_failed',
  'manual_handoff','policy_blocked','abandoned','error');

CREATE TYPE application_outcome AS ENUM (
  'pending','acknowledged','screening','interviewing','offer',
  'accepted','rejected','no_response','withdrawn');

CREATE TYPE approval_status AS ENUM (
  'pending','approved','rejected','changes_requested','superseded');

CREATE TYPE approval_kind AS ENUM (
  'application','resume','answers','cover_letter','policy_exception');

CREATE TYPE fact_type AS ENUM (
  'experience','education','certification','skill','project',
  'achievement','tool','contact','preference','authorization');

CREATE TYPE fact_status AS ENUM ('draft','verified','retired');

CREATE TYPE claim_verdict AS ENUM ('supported','overreach','unsupported','not_checked');

CREATE TYPE question_class AS ENUM (
  'factual_safe','derived','uncertain','judgement','sensitive','unanswerable');

CREATE TYPE answer_source AS ENUM ('agent','human','derived','blank');

CREATE TYPE submission_method AS ENUM ('official_api','email','human_handoff','blocked');

CREATE TYPE submission_result AS ENUM (
  'pending','succeeded','failed','handoff_required','blocked_by_policy');

CREATE TYPE ingestion_method AS ENUM (
  'official_api','public_feed','manual_import','extension_capture','blocked');

CREATE TYPE recommendation AS ENUM ('strong','moderate','weak','no');

CREATE TYPE agent_name AS ENUM ('resume_optimizer','job_application','monitoring');

CREATE TYPE run_status AS ENUM ('running','succeeded','failed','aborted');

CREATE TYPE alert_severity AS ENUM ('critical','high','medium','low','info');

-- ---------------------------------------------------- 3. identity and truth
CREATE TABLE users (
  id              UUID NOT NULL,
  email           CITEXT NOT NULL,
  password_hash   TEXT,
  totp_secret_enc BYTEA,
  role            VARCHAR(32) NOT NULL DEFAULT 'owner',
  last_login_at   TIMESTAMPTZ,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT pk_users PRIMARY KEY (id),
  CONSTRAINT uq_users_email UNIQUE (email),
  CONSTRAINT ck_users_role_allowed CHECK (role IN ('owner','reviewer'))
);

CREATE TABLE profiles (
  id                  UUID NOT NULL,
  user_id             UUID NOT NULL,
  full_name           TEXT NOT NULL,
  headline            TEXT,
  email               TEXT,
  phone               TEXT,
  location_city       TEXT,
  location_state      TEXT,
  location_country    TEXT,
  work_authorization  TEXT,
  willing_to_relocate BOOLEAN NOT NULL DEFAULT false,
  target_roles        TEXT[] NOT NULL DEFAULT '{}',
  min_salary          INTEGER,
  max_salary          INTEGER,
  links               JSONB NOT NULL DEFAULT '{}',
  version             INTEGER NOT NULL DEFAULT 1,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT pk_profiles PRIMARY KEY (id),
  CONSTRAINT uq_profiles_user_id UNIQUE (user_id),
  CONSTRAINT fk_profiles_user_id_users FOREIGN KEY (user_id)
    REFERENCES users (id) ON DELETE CASCADE,
  CONSTRAINT ck_profiles_salary_range_ordered
    CHECK (min_salary IS NULL OR max_salary IS NULL OR max_salary >= min_salary)
);

CREATE TABLE experiences (
  id              UUID NOT NULL,
  profile_id      UUID NOT NULL,
  company_name    TEXT NOT NULL,
  title           TEXT NOT NULL,
  employment_type TEXT,
  location        TEXT,
  is_remote       BOOLEAN NOT NULL DEFAULT false,
  start_date      DATE NOT NULL,
  end_date        DATE,
  is_current      BOOLEAN GENERATED ALWAYS AS ((end_date IS NULL)) STORED,
  description     TEXT,
  display_order   INTEGER NOT NULL DEFAULT 0,
  deleted_at      TIMESTAMPTZ,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT pk_experiences PRIMARY KEY (id),
  CONSTRAINT fk_experiences_profile_id_profiles FOREIGN KEY (profile_id)
    REFERENCES profiles (id) ON DELETE CASCADE,
  CONSTRAINT ck_experiences_dates_ordered
    CHECK (end_date IS NULL OR end_date >= start_date)
);
CREATE INDEX ix_experiences_profile_id ON experiences (profile_id);

CREATE TABLE educations (
  id             UUID NOT NULL,
  profile_id     UUID NOT NULL,
  institution    TEXT NOT NULL,
  degree         TEXT,
  field_of_study TEXT,
  start_date     DATE,
  end_date       DATE,
  gpa            DOUBLE PRECISION,
  location       TEXT,
  display_order  INTEGER NOT NULL DEFAULT 0,
  deleted_at     TIMESTAMPTZ,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT pk_educations PRIMARY KEY (id),
  CONSTRAINT fk_educations_profile_id_profiles FOREIGN KEY (profile_id)
    REFERENCES profiles (id) ON DELETE CASCADE,
  CONSTRAINT ck_educations_dates_ordered
    CHECK (end_date IS NULL OR start_date IS NULL OR end_date >= start_date),
  CONSTRAINT ck_educations_gpa_range CHECK (gpa IS NULL OR (gpa >= 0 AND gpa <= 5))
);
CREATE INDEX ix_educations_profile_id ON educations (profile_id);

CREATE TABLE certifications (
  id             UUID NOT NULL,
  profile_id     UUID NOT NULL,
  name           TEXT NOT NULL,
  issuer         TEXT,
  issued_date    DATE,
  expires_date   DATE,
  credential_id  TEXT,
  credential_url TEXT,
  status         VARCHAR(16) NOT NULL DEFAULT 'active',
  display_order  INTEGER NOT NULL DEFAULT 0,
  deleted_at     TIMESTAMPTZ,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT pk_certifications PRIMARY KEY (id),
  CONSTRAINT fk_certifications_profile_id_profiles FOREIGN KEY (profile_id)
    REFERENCES profiles (id) ON DELETE CASCADE,
  CONSTRAINT ck_certifications_status_allowed
    CHECK (status IN ('active','expired','in_progress')),
  -- An in-progress certification has no issue date, so it can never be
  -- rendered as one that is held.
  CONSTRAINT ck_certifications_in_progress_has_no_issue_date
    CHECK (status <> 'in_progress' OR issued_date IS NULL)
);
CREATE INDEX ix_certifications_profile_id ON certifications (profile_id);

CREATE TABLE skills (
  id                      UUID NOT NULL,
  profile_id              UUID NOT NULL,
  name                    TEXT NOT NULL,
  category                TEXT,
  proficiency             VARCHAR(16),
  first_used_date         DATE,
  last_used_date          DATE,
  evidence_experience_ids UUID[] NOT NULL DEFAULT '{}',
  display_order           INTEGER NOT NULL DEFAULT 0,
  deleted_at              TIMESTAMPTZ,
  created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT pk_skills PRIMARY KEY (id),
  CONSTRAINT uq_skills_profile_id_name UNIQUE (profile_id, name),
  CONSTRAINT fk_skills_profile_id_profiles FOREIGN KEY (profile_id)
    REFERENCES profiles (id) ON DELETE CASCADE,
  CONSTRAINT ck_skills_proficiency_allowed
    CHECK (proficiency IS NULL OR proficiency IN ('working','proficient','expert'))
);
CREATE INDEX ix_skills_profile_id ON skills (profile_id);

CREATE TABLE projects (
  id            UUID NOT NULL,
  profile_id    UUID NOT NULL,
  name          TEXT NOT NULL,
  summary       TEXT,
  role          TEXT,
  tech_stack    TEXT[] NOT NULL DEFAULT '{}',
  repo_url      TEXT,
  demo_url      TEXT,
  start_date    DATE,
  end_date      DATE,
  display_order INTEGER NOT NULL DEFAULT 0,
  deleted_at    TIMESTAMPTZ,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT pk_projects PRIMARY KEY (id),
  CONSTRAINT fk_projects_profile_id_profiles FOREIGN KEY (profile_id)
    REFERENCES profiles (id) ON DELETE CASCADE,
  CONSTRAINT ck_projects_dates_ordered
    CHECK (end_date IS NULL OR start_date IS NULL OR end_date >= start_date)
);
CREATE INDEX ix_projects_profile_id ON projects (profile_id);

-- ------------------------------------------------- profile_facts (grounding)
CREATE TABLE profile_facts (
  id           UUID NOT NULL,
  profile_id   UUID NOT NULL,
  fact_type    fact_type NOT NULL,
  text         TEXT NOT NULL,
  structured   JSONB NOT NULL DEFAULT '{}',
  source_table TEXT NOT NULL,
  source_id    UUID NOT NULL,
  status       fact_status NOT NULL DEFAULT 'draft',
  confidence   TEXT,
  valid_from   DATE,
  valid_to     DATE,
  embedding    vector(1024),
  usage_count  INTEGER NOT NULL DEFAULT 0,
  verified_by  UUID,
  verified_at  TIMESTAMPTZ,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  deleted_at   TIMESTAMPTZ,
  CONSTRAINT pk_profile_facts PRIMARY KEY (id),
  CONSTRAINT fk_profile_facts_profile_id_profiles FOREIGN KEY (profile_id)
    REFERENCES profiles (id) ON DELETE CASCADE,
  CONSTRAINT fk_profile_facts_verified_by_users FOREIGN KEY (verified_by)
    REFERENCES users (id) ON DELETE SET NULL,
  CONSTRAINT ck_profile_facts_source_table_allowed CHECK (source_table IN
    ('experiences','educations','certifications','skills','projects','profiles')),
  -- "Verified" must name a human and a moment, or it is only a word.
  CONSTRAINT ck_profile_facts_verified_requires_actor
    CHECK (status <> 'verified' OR (verified_by IS NOT NULL AND verified_at IS NOT NULL)),
  CONSTRAINT ck_profile_facts_text_not_blank CHECK (length(btrim(text)) > 0),
  CONSTRAINT ck_profile_facts_usage_count_non_negative CHECK (usage_count >= 0),
  CONSTRAINT ck_profile_facts_validity_ordered
    CHECK (valid_to IS NULL OR valid_from IS NULL OR valid_to >= valid_from)
);
CREATE INDEX idx_facts_profile_status ON profile_facts (profile_id, status)
  WHERE deleted_at IS NULL;
CREATE INDEX idx_facts_type ON profile_facts (fact_type);
CREATE INDEX idx_facts_embedding ON profile_facts
  USING hnsw (embedding vector_cosine_ops);
CREATE INDEX idx_facts_text_trgm ON profile_facts USING gin (text gin_trgm_ops);
-- DEVIATION D-2: the design's UNIQUE(source_table, source_id, text) is hashed
-- on `text` because a btree entry is capped near 2704 bytes and fact text is
-- free-form. Same uniqueness semantics, no length ceiling.
CREATE UNIQUE INDEX uq_facts_source ON profile_facts (source_table, source_id, md5(text))
  WHERE deleted_at IS NULL;

-- --------------------------------------------------------- 3b. job domain
CREATE TABLE companies (
  id              UUID NOT NULL,
  name            TEXT NOT NULL,
  normalized_name TEXT NOT NULL,
  domain          TEXT,
  industry        TEXT,
  size_band       TEXT,
  hq_location     TEXT,
  careers_url     TEXT,
  notes           TEXT,
  blocklisted     BOOLEAN NOT NULL DEFAULT false,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT pk_companies PRIMARY KEY (id),
  CONSTRAINT uq_companies_normalized_name UNIQUE (normalized_name)
);

CREATE TABLE site_policies (
  id                    UUID NOT NULL,
  domain                TEXT NOT NULL,
  automation_allowed    BOOLEAN NOT NULL DEFAULT false,
  ingestion_method      ingestion_method NOT NULL DEFAULT 'blocked',
  submission_method     submission_method NOT NULL DEFAULT 'human_handoff',
  max_requests_per_hour INTEGER NOT NULL DEFAULT 10,
  robots_respected      BOOLEAN NOT NULL DEFAULT true,
  requires_login        BOOLEAN NOT NULL DEFAULT true,
  tos_url               TEXT,
  notes                 TEXT,
  reviewed_by           UUID,
  reviewed_at           TIMESTAMPTZ,
  next_review_due       DATE,
  created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT pk_site_policies PRIMARY KEY (id),
  CONSTRAINT uq_site_policies_domain UNIQUE (domain),
  CONSTRAINT fk_site_policies_reviewed_by_users FOREIGN KEY (reviewed_by)
    REFERENCES users (id) ON DELETE SET NULL,
  CONSTRAINT ck_site_policies_rate_limit_positive CHECK (max_requests_per_hour > 0),
  -- Automation may only be switched on by a human who recorded a ToS review.
  CONSTRAINT ck_site_policies_automation_requires_review
    CHECK (automation_allowed = false
           OR (reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL))
);

CREATE TABLE sources (
  id               UUID NOT NULL,
  name             TEXT NOT NULL,
  slug             TEXT NOT NULL,
  domain           TEXT NOT NULL,
  ingestion_method ingestion_method NOT NULL DEFAULT 'blocked',
  config           JSONB NOT NULL DEFAULT '{}',
  enabled          BOOLEAN NOT NULL DEFAULT false,
  last_run_at      TIMESTAMPTZ,
  last_success_at  TIMESTAMPTZ,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT pk_sources PRIMARY KEY (id),
  CONSTRAINT uq_sources_slug UNIQUE (slug)
);
CREATE INDEX ix_sources_domain ON sources (domain);

CREATE TABLE jobs (
  id                UUID NOT NULL,
  source_id         UUID NOT NULL,
  company_id        UUID,
  external_id       TEXT,
  url               TEXT NOT NULL,
  title             TEXT NOT NULL,
  normalized_title  TEXT,
  seniority         VARCHAR(16),
  location_raw      TEXT,
  location_city     TEXT,
  location_state    TEXT,
  location_country  TEXT,
  is_remote         BOOLEAN NOT NULL DEFAULT false,
  is_hybrid         BOOLEAN NOT NULL DEFAULT false,
  employment_type   TEXT,
  salary_min        INTEGER,
  salary_max        INTEGER,
  salary_currency   VARCHAR(3),
  salary_period     VARCHAR(16),
  description_raw   TEXT,
  description_hash  VARCHAR(64),
  requirements      JSONB NOT NULL DEFAULT '{}',
  posted_at         TIMESTAMPTZ,
  expires_at        TIMESTAMPTZ,
  first_seen_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  status            job_status NOT NULL DEFAULT 'discovered',
  discard_reason    TEXT,
  duplicate_of_id   UUID,
  priority          INTEGER NOT NULL DEFAULT 0,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT pk_jobs PRIMARY KEY (id),
  CONSTRAINT fk_jobs_source_id_sources FOREIGN KEY (source_id)
    REFERENCES sources (id) ON DELETE RESTRICT,
  CONSTRAINT fk_jobs_company_id_companies FOREIGN KEY (company_id)
    REFERENCES companies (id) ON DELETE SET NULL,
  CONSTRAINT fk_jobs_duplicate_of_id_jobs FOREIGN KEY (duplicate_of_id)
    REFERENCES jobs (id) ON DELETE SET NULL,
  CONSTRAINT ck_jobs_salary_range_ordered
    CHECK (salary_min IS NULL OR salary_max IS NULL OR salary_max >= salary_min),
  CONSTRAINT ck_jobs_not_own_duplicate
    CHECK (duplicate_of_id IS NULL OR duplicate_of_id <> id),
  CONSTRAINT ck_jobs_duplicate_has_canonical
    CHECK (status <> 'duplicate' OR duplicate_of_id IS NOT NULL)
);
CREATE UNIQUE INDEX uq_jobs_source_external ON jobs (source_id, external_id)
  WHERE external_id IS NOT NULL;
CREATE INDEX idx_jobs_status_priority ON jobs (status, priority DESC, posted_at DESC);
CREATE INDEX idx_jobs_dedupe ON jobs (company_id, normalized_title, description_hash);
CREATE INDEX idx_jobs_title_trgm ON jobs USING gin (normalized_title gin_trgm_ops);

CREATE TABLE job_raw_snapshots (
  id           UUID NOT NULL,
  job_id       UUID NOT NULL,
  captured_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  content      TEXT,
  content_type VARCHAR(64),
  http_status  INTEGER,
  source_url   TEXT,
  CONSTRAINT pk_job_raw_snapshots PRIMARY KEY (id),
  CONSTRAINT fk_job_raw_snapshots_job_id_jobs FOREIGN KEY (job_id)
    REFERENCES jobs (id) ON DELETE CASCADE
);
CREATE INDEX ix_job_raw_snapshots_job_id ON job_raw_snapshots (job_id);

-- ------------------------------------------------------- 3c. operations
CREATE TABLE events (
  id             UUID NOT NULL,
  type           TEXT NOT NULL,
  version        INTEGER NOT NULL DEFAULT 1,
  aggregate_type TEXT NOT NULL,
  aggregate_id   UUID,
  correlation_id UUID NOT NULL,
  causation_id   UUID,
  actor_type     VARCHAR(16) NOT NULL,
  actor_id       TEXT,
  trace_id       VARCHAR(64),
  payload        JSONB NOT NULL,
  occurred_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  published      BOOLEAN NOT NULL DEFAULT false,
  published_at   TIMESTAMPTZ,
  CONSTRAINT pk_events PRIMARY KEY (id),
  CONSTRAINT ck_events_actor_type_allowed
    CHECK (actor_type IN ('agent','human','system')),
  CONSTRAINT ck_events_aggregate_type_allowed CHECK (aggregate_type IN
    ('job','application','resume','approval','submission','system'))
);
CREATE INDEX idx_events_unpublished ON events (occurred_at) WHERE published = false;
CREATE INDEX idx_events_aggregate ON events (aggregate_type, aggregate_id, occurred_at DESC);
CREATE INDEX idx_events_correlation ON events (correlation_id, occurred_at);
CREATE INDEX idx_events_type_time ON events (type, occurred_at DESC);

CREATE TABLE processed_events (
  event_id       UUID NOT NULL,
  consumer_group TEXT NOT NULL,
  processed_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  result         TEXT,
  CONSTRAINT pk_processed_events PRIMARY KEY (event_id, consumer_group)
);
CREATE INDEX ix_processed_events_processed_at ON processed_events (processed_at);

CREATE TABLE agent_runs (
  id               UUID NOT NULL,
  agent            agent_name NOT NULL,
  pipeline         VARCHAR(32) NOT NULL,
  trigger_event_id UUID,
  aggregate_type   TEXT,
  aggregate_id     UUID,
  correlation_id   UUID,
  status           run_status NOT NULL DEFAULT 'running',
  steps_total      INTEGER NOT NULL DEFAULT 0,
  steps_completed  INTEGER NOT NULL DEFAULT 0,
  input_tokens     INTEGER NOT NULL DEFAULT 0,
  output_tokens    INTEGER NOT NULL DEFAULT 0,
  cost_usd         NUMERIC(12,4) NOT NULL DEFAULT 0,
  duration_ms      INTEGER,
  error_type       TEXT,
  error_message    TEXT,
  prompt_version   TEXT,
  started_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at      TIMESTAMPTZ,
  CONSTRAINT pk_agent_runs PRIMARY KEY (id),
  CONSTRAINT fk_agent_runs_trigger_event_id_events FOREIGN KEY (trigger_event_id)
    REFERENCES events (id) ON DELETE SET NULL,
  CONSTRAINT ck_agent_runs_pipeline_allowed CHECK (pipeline IN
    ('evaluation','tailoring','packet','submission','digest')),
  CONSTRAINT ck_agent_runs_cost_non_negative CHECK (cost_usd >= 0),
  CONSTRAINT ck_agent_runs_steps_consistent
    CHECK (steps_completed <= steps_total OR steps_total = 0)
);
CREATE INDEX ix_agent_runs_agent_started ON agent_runs (agent, started_at DESC);
CREATE INDEX ix_agent_runs_correlation_id ON agent_runs (correlation_id);

CREATE TABLE agent_run_steps (
  id                UUID NOT NULL,
  run_id            UUID NOT NULL,
  step_index        INTEGER NOT NULL,
  step_name         TEXT NOT NULL,
  status            run_status NOT NULL DEFAULT 'running',
  input_digest      VARCHAR(64),
  output            JSONB NOT NULL DEFAULT '{}',
  guardrail_results JSONB NOT NULL DEFAULT '{}',
  duration_ms       INTEGER,
  started_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at       TIMESTAMPTZ,
  CONSTRAINT pk_agent_run_steps PRIMARY KEY (id),
  CONSTRAINT uq_agent_run_steps_run_id_step_index UNIQUE (run_id, step_index),
  CONSTRAINT fk_agent_run_steps_run_id_agent_runs FOREIGN KEY (run_id)
    REFERENCES agent_runs (id) ON DELETE CASCADE
);

CREATE TABLE llm_calls (
  id                UUID NOT NULL,
  run_id            UUID,
  step_id           UUID,
  model             TEXT NOT NULL,
  input_tokens      INTEGER NOT NULL DEFAULT 0,
  output_tokens     INTEGER NOT NULL DEFAULT 0,
  cost_usd          NUMERIC(12,4) NOT NULL DEFAULT 0,
  latency_ms        INTEGER,
  stop_reason       VARCHAR(32),
  prompt_hash       VARCHAR(64),
  prompt_redacted   TEXT,
  response_redacted TEXT,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT pk_llm_calls PRIMARY KEY (id),
  CONSTRAINT fk_llm_calls_run_id_agent_runs FOREIGN KEY (run_id)
    REFERENCES agent_runs (id) ON DELETE CASCADE,
  CONSTRAINT fk_llm_calls_step_id_agent_run_steps FOREIGN KEY (step_id)
    REFERENCES agent_run_steps (id) ON DELETE SET NULL,
  CONSTRAINT ck_llm_calls_cost_non_negative CHECK (cost_usd >= 0)
);
CREATE INDEX ix_llm_calls_created_at ON llm_calls (created_at DESC);

CREATE TABLE job_evaluations (
  id                   UUID NOT NULL,
  job_id               UUID NOT NULL,
  profile_version      INTEGER NOT NULL,
  score                NUMERIC(5,2) NOT NULL,
  recommendation       recommendation NOT NULL,
  match_matrix         JSONB NOT NULL,
  matched_requirements JSONB NOT NULL DEFAULT '{}',
  missing_requirements JSONB NOT NULL DEFAULT '{}',
  gaps                 TEXT[] NOT NULL DEFAULT '{}',
  rationale            TEXT,
  score_breakdown      JSONB NOT NULL DEFAULT '{}',
  agent_run_id         UUID,
  model                TEXT,
  prompt_version       TEXT,
  created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT pk_job_evaluations PRIMARY KEY (id),
  CONSTRAINT fk_job_evaluations_job_id_jobs FOREIGN KEY (job_id)
    REFERENCES jobs (id) ON DELETE CASCADE,
  CONSTRAINT fk_job_evaluations_agent_run_id_agent_runs FOREIGN KEY (agent_run_id)
    REFERENCES agent_runs (id) ON DELETE SET NULL,
  CONSTRAINT ck_job_evaluations_score_range CHECK (score >= 0 AND score <= 100)
);
CREATE INDEX ix_job_evaluations_job_id ON job_evaluations (job_id, created_at DESC);

-- ------------------------------------------------- 3d. application domain
CREATE TABLE applications (
  id                 UUID NOT NULL,
  job_id             UUID NOT NULL,
  profile_id         UUID NOT NULL,
  evaluation_id      UUID,
  status             application_status NOT NULL DEFAULT 'created',
  outcome            application_outcome NOT NULL DEFAULT 'pending',
  resume_id          UUID,
  cover_letter_id    UUID,
  submission_method  submission_method,
  priority           INTEGER NOT NULL DEFAULT 0,
  approved_at        TIMESTAMPTZ,
  approved_by        UUID,
  submitted_at       TIMESTAMPTZ,
  outcome_updated_at TIMESTAMPTZ,
  error_reason       TEXT,
  abandon_reason     TEXT,
  notes              TEXT,
  version            INTEGER NOT NULL DEFAULT 1,
  created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT pk_applications PRIMARY KEY (id),
  CONSTRAINT fk_applications_job_id_jobs FOREIGN KEY (job_id)
    REFERENCES jobs (id) ON DELETE RESTRICT,
  CONSTRAINT fk_applications_profile_id_profiles FOREIGN KEY (profile_id)
    REFERENCES profiles (id) ON DELETE RESTRICT,
  CONSTRAINT fk_applications_evaluation_id_job_evaluations FOREIGN KEY (evaluation_id)
    REFERENCES job_evaluations (id) ON DELETE SET NULL,
  CONSTRAINT fk_applications_approved_by_users FOREIGN KEY (approved_by)
    REFERENCES users (id) ON DELETE RESTRICT,
  -- THE constraint. A bug in application code cannot produce a submitted
  -- application without a recorded human approver.
  CONSTRAINT ck_applications_approved_requires_human CHECK (
    status NOT IN ('approved','submission_ready','submitting','submitted','manual_handoff')
    OR (approved_by IS NOT NULL AND approved_at IS NOT NULL)
  ),
  CONSTRAINT ck_applications_submitted_requires_timestamp
    CHECK (status <> 'submitted' OR submitted_at IS NOT NULL)
);
CREATE UNIQUE INDEX uq_application_per_job ON applications (job_id)
  WHERE status NOT IN ('abandoned','error','policy_blocked');
CREATE INDEX idx_applications_status ON applications (status, updated_at DESC);
CREATE INDEX idx_applications_outcome ON applications (outcome, submitted_at DESC);

CREATE TABLE resumes (
  id                  UUID NOT NULL,
  profile_id          UUID NOT NULL,
  application_id      UUID,
  kind                VARCHAR(16) NOT NULL,
  version             INTEGER NOT NULL DEFAULT 1,
  content             JSONB NOT NULL,
  summary_text        TEXT,
  diff_vs_base        JSONB NOT NULL DEFAULT '{}',
  verification_status VARCHAR(16) NOT NULL DEFAULT 'pending',
  verification_report JSONB NOT NULL DEFAULT '{}',
  agent_run_id        UUID,
  model               TEXT,
  prompt_version      TEXT,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT pk_resumes PRIMARY KEY (id),
  CONSTRAINT fk_resumes_profile_id_profiles FOREIGN KEY (profile_id)
    REFERENCES profiles (id) ON DELETE CASCADE,
  CONSTRAINT fk_resumes_application_id_applications FOREIGN KEY (application_id)
    REFERENCES applications (id) ON DELETE CASCADE,
  CONSTRAINT fk_resumes_agent_run_id_agent_runs FOREIGN KEY (agent_run_id)
    REFERENCES agent_runs (id) ON DELETE SET NULL,
  CONSTRAINT ck_resumes_kind_allowed CHECK (kind IN ('base','tailored')),
  CONSTRAINT ck_resumes_verification_status_allowed
    CHECK (verification_status IN ('pending','passed','failed')),
  CONSTRAINT ck_resumes_tailored_has_application
    CHECK (kind <> 'tailored' OR application_id IS NOT NULL)
);
CREATE INDEX ix_resumes_application_id ON resumes (application_id);

CREATE TABLE resume_claims (
  id             UUID NOT NULL,
  resume_id      UUID NOT NULL,
  section        TEXT NOT NULL,
  path           TEXT NOT NULL,
  claim_text     TEXT NOT NULL,
  fact_ids       UUID[] NOT NULL,
  numerals_used  TEXT[] NOT NULL DEFAULT '{}',
  verdict        claim_verdict NOT NULL DEFAULT 'not_checked',
  verdict_reason TEXT,
  verified_at    TIMESTAMPTZ,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT pk_resume_claims PRIMARY KEY (id),
  CONSTRAINT fk_resume_claims_resume_id_resumes FOREIGN KEY (resume_id)
    REFERENCES resumes (id) ON DELETE CASCADE,
  -- An uncited claim cannot be persisted at all.
  CONSTRAINT ck_resume_claims_claim_has_facts CHECK (cardinality(fact_ids) > 0)
);
CREATE INDEX idx_claims_resume ON resume_claims (resume_id, verdict);

CREATE TABLE cover_letters (
  id                  UUID NOT NULL,
  profile_id          UUID NOT NULL,
  application_id      UUID,
  version             INTEGER NOT NULL DEFAULT 1,
  content             JSONB NOT NULL,
  body_text           TEXT,
  verification_status VARCHAR(16) NOT NULL DEFAULT 'pending',
  verification_report JSONB NOT NULL DEFAULT '{}',
  agent_run_id        UUID,
  model               TEXT,
  prompt_version      TEXT,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT pk_cover_letters PRIMARY KEY (id),
  CONSTRAINT fk_cover_letters_profile_id_profiles FOREIGN KEY (profile_id)
    REFERENCES profiles (id) ON DELETE CASCADE,
  CONSTRAINT fk_cover_letters_application_id_applications FOREIGN KEY (application_id)
    REFERENCES applications (id) ON DELETE CASCADE,
  CONSTRAINT fk_cover_letters_agent_run_id_agent_runs FOREIGN KEY (agent_run_id)
    REFERENCES agent_runs (id) ON DELETE SET NULL,
  CONSTRAINT ck_cover_letters_verification_status_allowed
    CHECK (verification_status IN ('pending','passed','failed'))
);
CREATE INDEX ix_cover_letters_application_id ON cover_letters (application_id);

CREATE TABLE cover_letter_claims (
  id              UUID NOT NULL,
  cover_letter_id UUID NOT NULL,
  section         TEXT NOT NULL,
  path            TEXT NOT NULL,
  claim_text      TEXT NOT NULL,
  fact_ids        UUID[] NOT NULL,
  numerals_used   TEXT[] NOT NULL DEFAULT '{}',
  verdict         claim_verdict NOT NULL DEFAULT 'not_checked',
  verdict_reason  TEXT,
  verified_at     TIMESTAMPTZ,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT pk_cover_letter_claims PRIMARY KEY (id),
  CONSTRAINT fk_cover_letter_claims_cover_letter_id_cover_letters
    FOREIGN KEY (cover_letter_id) REFERENCES cover_letters (id) ON DELETE CASCADE,
  CONSTRAINT ck_cover_letter_claims_claim_has_facts CHECK (cardinality(fact_ids) > 0)
);
CREATE INDEX idx_cover_letter_claims_letter ON cover_letter_claims (cover_letter_id, verdict);

-- Deferred FKs: applications <-> resumes / cover_letters is a genuine cycle.
ALTER TABLE applications ADD CONSTRAINT fk_applications_resume_id_resumes
  FOREIGN KEY (resume_id) REFERENCES resumes (id) ON DELETE SET NULL;
ALTER TABLE applications ADD CONSTRAINT fk_applications_cover_letter_id_cover_letters
  FOREIGN KEY (cover_letter_id) REFERENCES cover_letters (id) ON DELETE SET NULL;

CREATE TABLE application_questions (
  id                UUID NOT NULL,
  application_id    UUID NOT NULL,
  external_field_id TEXT,
  question_text     TEXT NOT NULL,
  input_type        VARCHAR(16) NOT NULL,
  options           JSONB NOT NULL DEFAULT '{}',
  is_required       BOOLEAN NOT NULL DEFAULT false,
  max_length        INTEGER,
  display_order     INTEGER NOT NULL DEFAULT 0,
  question_class    question_class NOT NULL,
  class_reason      TEXT,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT pk_application_questions PRIMARY KEY (id),
  CONSTRAINT fk_application_questions_application_id_applications
    FOREIGN KEY (application_id) REFERENCES applications (id) ON DELETE CASCADE,
  CONSTRAINT ck_application_questions_input_type_allowed CHECK (input_type IN
    ('text','textarea','select','multiselect','boolean','file','date','number'))
);
CREATE INDEX ix_application_questions_application_id
  ON application_questions (application_id, display_order);

CREATE TABLE application_answers (
  id                      UUID NOT NULL,
  question_id             UUID NOT NULL,
  application_id          UUID NOT NULL,
  value                   TEXT,
  value_structured        JSONB NOT NULL DEFAULT '{}',
  source                  answer_source NOT NULL,
  fact_ids                UUID[] NOT NULL DEFAULT '{}',
  confidence              NUMERIC(4,3),
  requires_human          BOOLEAN NOT NULL DEFAULT true,
  answered_by             UUID,
  edited_from_agent_value TEXT,
  created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT pk_application_answers PRIMARY KEY (id),
  CONSTRAINT uq_application_answers_question_id UNIQUE (question_id),
  CONSTRAINT fk_application_answers_question_id_application_questions
    FOREIGN KEY (question_id) REFERENCES application_questions (id) ON DELETE CASCADE,
  CONSTRAINT fk_application_answers_application_id_applications
    FOREIGN KEY (application_id) REFERENCES applications (id) ON DELETE CASCADE,
  CONSTRAINT fk_application_answers_answered_by_users FOREIGN KEY (answered_by)
    REFERENCES users (id) ON DELETE SET NULL,
  CONSTRAINT ck_application_answers_sensitive_human_only
    CHECK (NOT (requires_human = true AND source = 'agent' AND answered_by IS NULL)),
  CONSTRAINT ck_application_answers_confidence_range
    CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1))
);
CREATE INDEX ix_application_answers_application_id ON application_answers (application_id);

CREATE TABLE approvals (
  id                UUID NOT NULL,
  application_id    UUID NOT NULL,
  kind              approval_kind NOT NULL,
  status            approval_status NOT NULL DEFAULT 'pending',
  subject_snapshot  JSONB NOT NULL,
  guardrail_flags   JSONB NOT NULL DEFAULT '{}',
  decision_note     TEXT,
  changes_requested TEXT,
  edits_applied     JSONB NOT NULL DEFAULT '{}',
  requested_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  decided_at        TIMESTAMPTZ,
  decided_by        UUID,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT pk_approvals PRIMARY KEY (id),
  CONSTRAINT fk_approvals_application_id_applications FOREIGN KEY (application_id)
    REFERENCES applications (id) ON DELETE CASCADE,
  CONSTRAINT fk_approvals_decided_by_users FOREIGN KEY (decided_by)
    REFERENCES users (id) ON DELETE RESTRICT,
  CONSTRAINT ck_approvals_decided_has_actor CHECK (
    status IN ('pending','superseded')
    OR (decided_by IS NOT NULL AND decided_at IS NOT NULL))
);
CREATE UNIQUE INDEX uq_one_pending_approval ON approvals (application_id, kind)
  WHERE status = 'pending';
CREATE INDEX idx_approvals_pending ON approvals (requested_at) WHERE status = 'pending';

CREATE TABLE artifacts (
  id             UUID NOT NULL,
  kind           VARCHAR(32) NOT NULL,
  application_id UUID,
  resume_id      UUID,
  storage_key    TEXT NOT NULL,
  bucket         TEXT NOT NULL,
  content_type   VARCHAR(128),
  size_bytes     INTEGER,
  sha256         VARCHAR(64),
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at     TIMESTAMPTZ,
  CONSTRAINT pk_artifacts PRIMARY KEY (id),
  CONSTRAINT uq_artifacts_bucket_storage_key UNIQUE (bucket, storage_key),
  CONSTRAINT fk_artifacts_application_id_applications FOREIGN KEY (application_id)
    REFERENCES applications (id) ON DELETE CASCADE,
  CONSTRAINT fk_artifacts_resume_id_resumes FOREIGN KEY (resume_id)
    REFERENCES resumes (id) ON DELETE CASCADE,
  CONSTRAINT ck_artifacts_kind_allowed CHECK (kind IN
    ('resume_pdf','resume_docx','cover_letter_pdf','handoff_packet','evidence'))
);

CREATE TABLE submissions (
  id                 UUID NOT NULL,
  application_id     UUID NOT NULL,
  attempt            INTEGER NOT NULL,
  method             submission_method NOT NULL,
  target_domain      TEXT NOT NULL,
  policy_id          UUID,
  result             submission_result NOT NULL DEFAULT 'pending',
  http_status        INTEGER,
  external_reference TEXT,
  failure_reason     TEXT,
  blocked_reason     TEXT,
  packet_artifact_id UUID,
  started_at         TIMESTAMPTZ,
  finished_at        TIMESTAMPTZ,
  created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT pk_submissions PRIMARY KEY (id),
  CONSTRAINT uq_submissions_application_id_attempt UNIQUE (application_id, attempt),
  CONSTRAINT fk_submissions_application_id_applications FOREIGN KEY (application_id)
    REFERENCES applications (id) ON DELETE CASCADE,
  CONSTRAINT fk_submissions_policy_id_site_policies FOREIGN KEY (policy_id)
    REFERENCES site_policies (id) ON DELETE SET NULL,
  CONSTRAINT fk_submissions_packet_artifact_id_artifacts FOREIGN KEY (packet_artifact_id)
    REFERENCES artifacts (id) ON DELETE SET NULL,
  CONSTRAINT ck_submissions_attempt_positive CHECK (attempt > 0)
);
CREATE INDEX ix_submissions_application_id ON submissions (application_id);

CREATE TABLE application_status_history (
  id             UUID NOT NULL,
  application_id UUID NOT NULL,
  from_status    application_status,
  to_status      application_status NOT NULL,
  actor_type     VARCHAR(16) NOT NULL,
  actor_id       TEXT,
  event_id       UUID,
  reason         TEXT,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT pk_application_status_history PRIMARY KEY (id),
  CONSTRAINT fk_application_status_history_application_id_applications
    FOREIGN KEY (application_id) REFERENCES applications (id) ON DELETE CASCADE,
  CONSTRAINT ck_application_status_history_actor_type_allowed
    CHECK (actor_type IN ('agent','human','system'))
);
CREATE INDEX ix_application_status_history_application_id
  ON application_status_history (application_id, created_at DESC);

CREATE TABLE fact_evidence (
  id          UUID NOT NULL,
  fact_id     UUID NOT NULL,
  kind        VARCHAR(32) NOT NULL,
  artifact_id UUID,
  url         TEXT,
  note        TEXT,
  added_by    UUID,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT pk_fact_evidence PRIMARY KEY (id),
  CONSTRAINT fk_fact_evidence_fact_id_profile_facts FOREIGN KEY (fact_id)
    REFERENCES profile_facts (id) ON DELETE CASCADE,
  CONSTRAINT fk_fact_evidence_artifact_id_artifacts FOREIGN KEY (artifact_id)
    REFERENCES artifacts (id) ON DELETE SET NULL,
  CONSTRAINT fk_fact_evidence_added_by_users FOREIGN KEY (added_by)
    REFERENCES users (id) ON DELETE SET NULL,
  CONSTRAINT ck_fact_evidence_kind_allowed
    CHECK (kind IN ('document','url','attestation','credential')),
  CONSTRAINT ck_fact_evidence_has_some_evidence
    CHECK (artifact_id IS NOT NULL OR url IS NOT NULL OR note IS NOT NULL)
);
CREATE INDEX ix_fact_evidence_fact_id ON fact_evidence (fact_id);

-- ------------------------------------------------ 3e. remaining operations
CREATE TABLE daily_quotas (
  id          UUID NOT NULL,
  date        DATE NOT NULL,
  quota_type  VARCHAR(32) NOT NULL,
  limit_value INTEGER NOT NULL,
  used_value  INTEGER NOT NULL DEFAULT 0,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT pk_daily_quotas PRIMARY KEY (id),
  CONSTRAINT uq_daily_quotas_date_quota_type UNIQUE (date, quota_type),
  CONSTRAINT ck_daily_quotas_quota_type_allowed CHECK (quota_type IN
    ('applications','llm_cost','evaluations','submissions')),
  CONSTRAINT ck_daily_quotas_values_non_negative
    CHECK (used_value >= 0 AND limit_value >= 0)
);

CREATE TABLE metrics_snapshots (
  id           UUID NOT NULL,
  grain        VARCHAR(8) NOT NULL,
  bucket_start TIMESTAMPTZ NOT NULL,
  metrics      JSONB NOT NULL,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT pk_metrics_snapshots PRIMARY KEY (id),
  CONSTRAINT uq_metrics_snapshots_grain_bucket_start UNIQUE (grain, bucket_start),
  CONSTRAINT ck_metrics_snapshots_grain_allowed CHECK (grain IN ('5min','hour','day'))
);

CREATE TABLE alerts (
  id              UUID NOT NULL,
  kind            TEXT NOT NULL,
  severity        alert_severity NOT NULL,
  title           TEXT NOT NULL,
  detail          JSONB NOT NULL DEFAULT '{}',
  aggregate_type  TEXT,
  aggregate_id    UUID,
  status          VARCHAR(16) NOT NULL DEFAULT 'open',
  auto_paused     BOOLEAN NOT NULL DEFAULT false,
  acknowledged_by UUID,
  acknowledged_at TIMESTAMPTZ,
  resolved_at     TIMESTAMPTZ,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT pk_alerts PRIMARY KEY (id),
  CONSTRAINT fk_alerts_acknowledged_by_users FOREIGN KEY (acknowledged_by)
    REFERENCES users (id) ON DELETE SET NULL,
  CONSTRAINT ck_alerts_status_allowed CHECK (status IN ('open','acknowledged','resolved'))
);
CREATE INDEX idx_alerts_open ON alerts (created_at DESC) WHERE status = 'open';

CREATE TABLE notifications (
  id            UUID NOT NULL,
  channel       VARCHAR(16) NOT NULL,
  kind          TEXT NOT NULL,
  title         TEXT NOT NULL,
  body          TEXT,
  payload       JSONB NOT NULL DEFAULT '{}',
  read_at       TIMESTAMPTZ,
  dispatched_at TIMESTAMPTZ,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT pk_notifications PRIMARY KEY (id),
  CONSTRAINT ck_notifications_channel_allowed CHECK (channel IN ('in_app','email'))
);

CREATE TABLE audit_logs (
  id            UUID NOT NULL,
  actor_type    VARCHAR(16) NOT NULL,
  actor_id      TEXT,
  action        VARCHAR(16) NOT NULL,
  resource_type TEXT NOT NULL,
  resource_id   UUID,
  before        JSONB,
  after         JSONB,
  ip_address    INET,
  user_agent    TEXT,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT pk_audit_logs PRIMARY KEY (id),
  CONSTRAINT ck_audit_logs_action_allowed CHECK (action IN ('insert','update','delete'))
);
CREATE INDEX idx_audit_logs_resource ON audit_logs (resource_type, resource_id, created_at DESC);

CREATE TABLE settings (
  key         TEXT NOT NULL,
  value       JSONB NOT NULL,
  description TEXT,
  updated_by  UUID,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT pk_settings PRIMARY KEY (key),
  CONSTRAINT fk_settings_updated_by_users FOREIGN KEY (updated_by)
    REFERENCES users (id) ON DELETE SET NULL
);

-- =====================================================================
-- 6. Triggers: invariants a CHECK constraint cannot express.
--
-- Every one of these enforces something the architecture states as a
-- guarantee. None of them is a convenience, and none may be dropped to
-- make a test pass.
-- =====================================================================

-- --------------------------------------------------------- updated_at
CREATE OR REPLACE FUNCTION jobforge_set_updated_at() RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DO $$
DECLARE t TEXT;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'users','profiles','experiences','educations','certifications','skills',
    'projects','profile_facts','companies','site_policies','sources','jobs',
    'applications','resumes','cover_letters','application_answers','approvals',
    'daily_quotas','settings'
  ] LOOP
    EXECUTE format(
      'CREATE TRIGGER trg_%1$s_set_updated_at BEFORE UPDATE ON %1$s
       FOR EACH ROW EXECUTE FUNCTION jobforge_set_updated_at()', t);
  END LOOP;
END $$;

-- ------------------------------------------- verified facts are immutable
-- SECURITY.md §4.1. Grants stop an agent writing here at all. This trigger
-- additionally stops ANY role - including app_api and a careless human -
-- from silently altering the substance of an already-verified fact. To
-- change what a verified fact says, it must first return to 'draft' and be
-- re-verified by a human.
CREATE OR REPLACE FUNCTION jobforge_protect_verified_facts() RETURNS TRIGGER AS $$
BEGIN
  IF OLD.status = 'verified'
     AND NEW.status = 'verified'
     AND (NEW.text          IS DISTINCT FROM OLD.text
       OR NEW.structured    IS DISTINCT FROM OLD.structured
       OR NEW.fact_type     IS DISTINCT FROM OLD.fact_type
       OR NEW.source_table  IS DISTINCT FROM OLD.source_table
       OR NEW.source_id     IS DISTINCT FROM OLD.source_id)
  THEN
    RAISE EXCEPTION
      'cannot modify the substance of a verified fact (%); set status to draft and re-verify',
      OLD.id
      USING ERRCODE = 'raise_exception';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_profile_facts_protect_verified
  BEFORE UPDATE ON profile_facts
  FOR EACH ROW EXECUTE FUNCTION jobforge_protect_verified_facts();

-- ------------------------------------------ claims cite verified facts only
-- fact_ids is a UUID[], so PostgreSQL cannot enforce referential integrity
-- on it. This trigger does that job: every cited fact must exist, belong to
-- an undeleted row, and be VERIFIED. A claim citing a draft fact is exactly
-- the fabrication path the architecture exists to close.
CREATE OR REPLACE FUNCTION jobforge_check_claim_facts() RETURNS TRIGGER AS $$
DECLARE
  cited   INTEGER;
  present INTEGER;
BEGIN
  cited := cardinality(NEW.fact_ids);

  SELECT count(*) INTO present
  FROM profile_facts f
  WHERE f.id = ANY (NEW.fact_ids)
    AND f.status = 'verified'
    AND f.deleted_at IS NULL;

  IF present <> cited THEN
    RAISE EXCEPTION
      'claim cites % fact id(s) but only % are verified and present', cited, present
      USING ERRCODE = 'foreign_key_violation';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_resume_claims_facts_verified
  BEFORE INSERT OR UPDATE ON resume_claims
  FOR EACH ROW EXECUTE FUNCTION jobforge_check_claim_facts();

CREATE TRIGGER trg_cover_letter_claims_facts_verified
  BEFORE INSERT OR UPDATE ON cover_letter_claims
  FOR EACH ROW EXECUTE FUNCTION jobforge_check_claim_facts();

-- ------------------------------------- approval must be a real human record
-- GAP FOUND DURING IMPLEMENTATION (see docs/DATABASE_IMPLEMENTATION.md, D-4).
-- ck_applications_approved_requires_human only requires approved_by to be
-- non-null. An agent role holding UPDATE on applications could satisfy it by
-- writing any user id. This trigger closes that: the approval must exist as
-- a row in `approvals`, which no agent role may write.
CREATE OR REPLACE FUNCTION jobforge_require_real_approval() RETURNS TRIGGER AS $$
DECLARE
  approval_count INTEGER;
BEGIN
  IF NEW.status IN ('approved','submission_ready','submitting','submitted','manual_handoff') THEN
    SELECT count(*) INTO approval_count
    FROM approvals a
    WHERE a.application_id = NEW.id
      AND a.status = 'approved'
      AND a.decided_by IS NOT NULL
      AND a.decided_at IS NOT NULL;

    IF approval_count = 0 THEN
      RAISE EXCEPTION
        'application % cannot enter status % without an approved approvals row '
        'decided by a human', NEW.id, NEW.status
        USING ERRCODE = 'raise_exception';
    END IF;

    IF NEW.approved_by IS NULL OR NEW.approved_at IS NULL THEN
      RAISE EXCEPTION 'application % is missing approved_by/approved_at', NEW.id
        USING ERRCODE = 'raise_exception';
    END IF;

    -- approved_by must match the human who actually decided.
    IF NOT EXISTS (
      SELECT 1 FROM approvals a
      WHERE a.application_id = NEW.id
        AND a.status = 'approved'
        AND a.decided_by = NEW.approved_by
    ) THEN
      RAISE EXCEPTION
        'applications.approved_by (%) does not match any approver of application %',
        NEW.approved_by, NEW.id
        USING ERRCODE = 'raise_exception';
    END IF;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_applications_require_real_approval
  BEFORE INSERT OR UPDATE ON applications
  FOR EACH ROW EXECUTE FUNCTION jobforge_require_real_approval();

-- ------------------------------------------------------- append-only tables
CREATE OR REPLACE FUNCTION jobforge_block_mutation() RETURNS TRIGGER AS $$
BEGIN
  RAISE EXCEPTION '% is append-only; % is not permitted', TG_TABLE_NAME, TG_OP
    USING ERRCODE = 'raise_exception';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_application_status_history_append_only
  BEFORE UPDATE OR DELETE ON application_status_history
  FOR EACH ROW EXECUTE FUNCTION jobforge_block_mutation();

CREATE TRIGGER trg_audit_logs_append_only
  BEFORE UPDATE OR DELETE ON audit_logs
  FOR EACH ROW EXECUTE FUNCTION jobforge_block_mutation();

CREATE TRIGGER trg_job_evaluations_append_only
  BEFORE UPDATE OR DELETE ON job_evaluations
  FOR EACH ROW EXECUTE FUNCTION jobforge_block_mutation();

-- events: the outbox relay must be able to flip `published`, and nothing else.
CREATE OR REPLACE FUNCTION jobforge_events_publish_only() RETURNS TRIGGER AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'events is append-only; DELETE is not permitted'
      USING ERRCODE = 'raise_exception';
  END IF;
  IF NEW.id             IS DISTINCT FROM OLD.id
     OR NEW.type        IS DISTINCT FROM OLD.type
     OR NEW.payload     IS DISTINCT FROM OLD.payload
     OR NEW.occurred_at IS DISTINCT FROM OLD.occurred_at
     OR NEW.aggregate_id IS DISTINCT FROM OLD.aggregate_id
     OR NEW.actor_type  IS DISTINCT FROM OLD.actor_type
  THEN
    RAISE EXCEPTION 'events rows are immutable except for publication state'
      USING ERRCODE = 'raise_exception';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_events_publish_only
  BEFORE UPDATE OR DELETE ON events
  FOR EACH ROW EXECUTE FUNCTION jobforge_events_publish_only();

-- --------------------------------------------- fact usage counter (SECURITY DEFINER)
-- Agents need to record that a fact was used, but must not hold UPDATE on
-- profile_facts. DATABASE_DESIGN.md §4.4 specifies exactly this escape hatch:
-- a narrow, definer-rights function that can touch one column and nothing else.
CREATE OR REPLACE FUNCTION jobforge_increment_fact_usage(p_fact_id UUID)
RETURNS INTEGER AS $$
DECLARE
  new_count INTEGER;
BEGIN
  UPDATE profile_facts
     SET usage_count = usage_count + 1
   WHERE id = p_fact_id
  RETURNING usage_count INTO new_count;
  RETURN new_count;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = public;

REVOKE ALL ON FUNCTION jobforge_increment_fact_usage(UUID) FROM PUBLIC;

-- --------------------------------------------------------------- audit trail
-- Populated by trigger so changes are captured even if they bypass the
-- application layer (SECURITY.md §9.1).
CREATE OR REPLACE FUNCTION jobforge_write_audit() RETURNS TRIGGER AS $$
DECLARE
  resource UUID;
BEGIN
  IF TG_OP = 'DELETE' THEN
    resource := (to_jsonb(OLD) ->> 'id')::UUID;
    INSERT INTO audit_logs (id, actor_type, actor_id, action, resource_type,
                            resource_id, before, after)
    VALUES (gen_random_uuid(), 'system', current_user, 'delete', TG_TABLE_NAME,
            resource, to_jsonb(OLD), NULL);
    RETURN OLD;
  ELSIF TG_OP = 'UPDATE' THEN
    resource := (to_jsonb(NEW) ->> 'id')::UUID;
    INSERT INTO audit_logs (id, actor_type, actor_id, action, resource_type,
                            resource_id, before, after)
    VALUES (gen_random_uuid(), 'system', current_user, 'update', TG_TABLE_NAME,
            resource, to_jsonb(OLD), to_jsonb(NEW));
    RETURN NEW;
  ELSE
    resource := (to_jsonb(NEW) ->> 'id')::UUID;
    INSERT INTO audit_logs (id, actor_type, actor_id, action, resource_type,
                            resource_id, before, after)
    VALUES (gen_random_uuid(), 'system', current_user, 'insert', TG_TABLE_NAME,
            resource, NULL, to_jsonb(NEW));
    RETURN NEW;
  END IF;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = public;

DO $$
DECLARE t TEXT;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'profiles','profile_facts','site_policies','applications','approvals'
  ] LOOP
    EXECUTE format(
      'CREATE TRIGGER trg_%1$s_audit AFTER INSERT OR UPDATE OR DELETE ON %1$s
       FOR EACH ROW EXECUTE FUNCTION jobforge_write_audit()', t);
  END LOOP;
END $$;

-- `settings` has a TEXT primary key named `key`, so it needs its own audit
-- trigger rather than the id-based one above.
CREATE OR REPLACE FUNCTION jobforge_write_audit_settings() RETURNS TRIGGER AS $$
BEGIN
  INSERT INTO audit_logs (id, actor_type, actor_id, action, resource_type,
                          resource_id, before, after)
  VALUES (gen_random_uuid(), 'system', current_user, lower(TG_OP), 'settings',
          NULL,
          CASE WHEN TG_OP = 'INSERT' THEN NULL ELSE to_jsonb(OLD) END,
          CASE WHEN TG_OP = 'DELETE' THEN NULL ELSE to_jsonb(NEW) END);
  RETURN COALESCE(NEW, OLD);
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = public;

CREATE TRIGGER trg_settings_audit AFTER INSERT OR UPDATE OR DELETE ON settings
  FOR EACH ROW EXECUTE FUNCTION jobforge_write_audit_settings();
