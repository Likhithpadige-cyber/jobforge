# DATABASE_DESIGN.md

**Project:** AI Job Application Platform (`jobforge`)
**Status:** Design — v1.0
**Engine:** PostgreSQL 16 · extensions: `pgcrypto`, `pg_trgm`, `vector` (pgvector), `btree_gin`
**Parent document:** [ARCHITECTURE.md](./ARCHITECTURE.md)

---

## 1. Conventions

| Convention | Rule |
|---|---|
| Primary keys | `UUID` (UUIDv7 generated in application code — time-ordered, index-friendly) |
| Naming | `snake_case`, plural table names, singular column names |
| Timestamps | `TIMESTAMPTZ`, always UTC; `created_at` and `updated_at` on every mutable table |
| Soft delete | Only on `profile_*` tables (`deleted_at`); everything else is hard-deleted or archived |
| Enums | Native PostgreSQL `ENUM` types (values below are canonical and mirror ARCHITECTURE.md §7) |
| JSON | `JSONB` for genuinely variable structures only, never as a substitute for a column |
| Foreign keys | Always declared; `ON DELETE RESTRICT` by default, `CASCADE` only for owned child rows |
| Money | `NUMERIC(12,4)` for USD costs; never `float` |
| Concurrency | `version INTEGER` column on `applications`, `resumes`, `profiles` for optimistic locking |
| Migrations | Alembic, expand/contract only — never a destructive change in the same release as the code that needs it |

---

## 2. Entity-relationship overview

```
users ──1:1── profiles ──1:N── experiences ──┐
                       ├──1:N── educations ──┤
                       ├──1:N── certifications ─┤──▶ profile_facts ──1:N── fact_evidence
                       ├──1:N── skills ──────┤          │
                       └──1:N── projects ────┘          │
                                                        │  (grounding)
sources ──1:N── jobs ──N:1── companies                  │
                 │  └──1:N── job_raw_snapshots          │
                 │  └──1:N── job_evaluations ───────────┤
                 │                                      │
                 └──1:0..1── applications               │
                               ├──1:1── resumes ──1:N── resume_claims ──N:1──┘
                               ├──1:0..1── cover_letters
                               ├──1:N── application_questions ──1:1── application_answers
                               ├──1:N── approvals
                               ├──1:N── submissions
                               └──1:N── application_status_history

site_policies (by domain) ◀── consulted by ingestion + submissions

OPERATIONS (no FK into domain except aggregate_id, intentionally loose):
events · processed_events · agent_runs · agent_run_steps · llm_calls
daily_quotas · metrics_snapshots · alerts · notifications · audit_logs · settings
```

---

## 3. Enum types

```sql
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

CREATE TYPE submission_method AS ENUM (
  'official_api','email','human_handoff','blocked');

CREATE TYPE submission_result AS ENUM (
  'pending','succeeded','failed','handoff_required','blocked_by_policy');

CREATE TYPE ingestion_method AS ENUM (
  'official_api','public_feed','manual_import','extension_capture','blocked');

CREATE TYPE recommendation AS ENUM ('strong','moderate','weak','no');

CREATE TYPE agent_name AS ENUM ('resume_optimizer','job_application','monitoring');

CREATE TYPE run_status AS ENUM ('running','succeeded','failed','aborted');

CREATE TYPE alert_severity AS ENUM ('critical','high','medium','low','info');
```

---

## 4. Identity and truth

### 4.1 `users`

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `email` | CITEXT UNIQUE NOT NULL | |
| `password_hash` | TEXT | argon2id |
| `totp_secret_enc` | BYTEA | encrypted at app layer |
| `role` | TEXT NOT NULL DEFAULT 'owner' | `owner` \| `reviewer` |
| `last_login_at` | TIMESTAMPTZ | |
| `created_at`, `updated_at` | TIMESTAMPTZ | |

Single-user in practice; modeled multi-user so approvals have a real `decided_by` and so RLS can be added without a migration of meaning.

### 4.2 `profiles`

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `user_id` | UUID FK → users UNIQUE | |
| `full_name`, `headline` | TEXT | |
| `email`, `phone` | TEXT | PII — redacted from all prompts |
| `location_city`, `location_state`, `location_country` | TEXT | |
| `work_authorization` | TEXT | Human-entered. Never inferred, never model-generated |
| `willing_to_relocate` | BOOLEAN | |
| `target_roles` | TEXT[] | e.g. Cloud Engineer, AWS Cloud Engineer, DevOps Engineer, Infrastructure Engineer |
| `min_salary`, `max_salary` | INTEGER | Used only to filter jobs, never auto-disclosed |
| `links` | JSONB | linkedin, github, portfolio |
| `version` | INTEGER | optimistic lock |
| `created_at`, `updated_at` | TIMESTAMPTZ | |

### 4.3 `experiences`

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `profile_id` | UUID FK → profiles ON DELETE CASCADE | |
| `company_name`, `title`, `employment_type` | TEXT | |
| `location`, `is_remote` | TEXT / BOOLEAN | |
| `start_date` | DATE NOT NULL | |
| `end_date` | DATE NULL | NULL = current |
| `is_current` | BOOLEAN GENERATED ALWAYS AS (end_date IS NULL) STORED | |
| `description` | TEXT | Human-authored ground truth |
| `display_order` | INTEGER | |
| `deleted_at` | TIMESTAMPTZ | |

`CHECK (end_date IS NULL OR end_date >= start_date)`

`educations`, `certifications`, `skills`, `projects` follow the same shape:

- **`educations`**: `institution`, `degree`, `field_of_study`, `start_date`, `end_date`, `gpa` (nullable, never inferred), `location`
- **`certifications`**: `name`, `issuer`, `issued_date`, `expires_date`, `credential_id`, `credential_url`, `status` (`active`/`expired`/`in_progress`) — **`in_progress` certifications are never presented as held**; the resume renderer excludes them from the Certifications section and may only list them under an explicitly labelled "In progress" heading when the human enables it
- **`skills`**: `name`, `category`, `proficiency` (`working`/`proficient`/`expert` — self-assessed by the human only), `first_used_date`, `last_used_date`, `evidence_experience_ids` UUID[]
- **`projects`**: `name`, `summary`, `role`, `tech_stack` TEXT[], `repo_url`, `demo_url`, `start_date`, `end_date`

### 4.4 `profile_facts` — the grounding index

The single table every generated claim must trace to.

```sql
CREATE TABLE profile_facts (
  id             UUID PRIMARY KEY,
  profile_id     UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  fact_type      fact_type NOT NULL,
  text           TEXT NOT NULL,              -- atomic, defensible statement
  structured     JSONB NOT NULL DEFAULT '{}',-- numerals, dates, entities, scope
  source_table   TEXT NOT NULL,              -- 'experiences' | 'educations' | ...
  source_id      UUID NOT NULL,              -- row this fact derives from
  status         fact_status NOT NULL DEFAULT 'draft',
  confidence     TEXT,                       -- human's own confidence note
  valid_from     DATE,
  valid_to       DATE,
  embedding      vector(1024),               -- for retrieval
  usage_count    INTEGER NOT NULL DEFAULT 0,
  verified_by    UUID REFERENCES users(id),
  verified_at    TIMESTAMPTZ,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  deleted_at     TIMESTAMPTZ
);

CREATE INDEX idx_facts_profile_status ON profile_facts(profile_id, status)
  WHERE deleted_at IS NULL;
CREATE INDEX idx_facts_type ON profile_facts(fact_type);
CREATE INDEX idx_facts_embedding ON profile_facts
  USING hnsw (embedding vector_cosine_ops);
CREATE INDEX idx_facts_text_trgm ON profile_facts USING gin (text gin_trgm_ops);
CREATE UNIQUE INDEX uq_facts_source ON profile_facts(source_table, source_id, text)
  WHERE deleted_at IS NULL;
```

**The critical constraint is not in the schema — it is in the grants:**

```sql
GRANT SELECT ON profile_facts TO app_agent;   -- read only, no INSERT/UPDATE/DELETE
GRANT SELECT, INSERT, UPDATE, DELETE ON profile_facts TO app_api;
```

Agent processes connect as `app_agent`. This makes "never invent experience" a database permission rather than a prompt instruction. `usage_count` is incremented through a `SECURITY DEFINER` function the agent role may execute, so the counter still works without granting UPDATE.

Only `status = 'verified'` facts are eligible for citation in a resume. `structured` holds every numeral that may legitimately appear in generated content — the numeral guard (AGENT_DESIGN.md §2.4 G4) checks against exactly this field.

### 4.5 `fact_evidence`

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `fact_id` | UUID FK → profile_facts ON DELETE CASCADE | |
| `kind` | TEXT | `document` \| `url` \| `attestation` \| `credential` |
| `artifact_id` | UUID FK → artifacts NULL | |
| `url`, `note` | TEXT | |
| `added_by` | UUID FK → users | |
| `created_at` | TIMESTAMPTZ | |

Lets the review UI answer "prove it" for any bullet in one click.

---

## 5. Job domain

### 5.1 `sources`

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `name`, `slug` | TEXT UNIQUE | |
| `domain` | TEXT NOT NULL | FK-by-value to `site_policies.domain` |
| `ingestion_method` | ingestion_method NOT NULL | |
| `config` | JSONB | endpoint, feed URL, query params (no secrets) |
| `enabled` | BOOLEAN NOT NULL DEFAULT false | Off until a policy review passes |
| `last_run_at`, `last_success_at` | TIMESTAMPTZ | |
| `created_at`, `updated_at` | TIMESTAMPTZ | |

### 5.2 `site_policies` — ToS / legality registry

```sql
CREATE TABLE site_policies (
  id                    UUID PRIMARY KEY,
  domain                TEXT UNIQUE NOT NULL,
  automation_allowed    BOOLEAN NOT NULL DEFAULT false,
  ingestion_method      ingestion_method NOT NULL DEFAULT 'blocked',
  submission_method     submission_method NOT NULL DEFAULT 'human_handoff',
  max_requests_per_hour INTEGER NOT NULL DEFAULT 10,
  robots_respected      BOOLEAN NOT NULL DEFAULT true,
  requires_login        BOOLEAN NOT NULL DEFAULT true,
  tos_url               TEXT,
  notes                 TEXT,
  reviewed_by           UUID REFERENCES users(id),
  reviewed_at           TIMESTAMPTZ,
  next_review_due       DATE,
  created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);
GRANT SELECT ON site_policies TO app_agent;  -- agents read, never write
```

Defaults are the most restrictive values. **The absence of a row means blocked** — the policy lookup function returns a synthetic blocked policy for unknown domains rather than raising, so there is no "fail open" path.

### 5.3 `companies`

`id`, `name`, `normalized_name` (UNIQUE), `domain`, `industry`, `size_band`, `hq_location`, `careers_url`, `notes`, `blocklisted BOOLEAN` (never apply here), `created_at`, `updated_at`.

### 5.4 `jobs`

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `source_id` | UUID FK → sources | |
| `company_id` | UUID FK → companies NULL | Resolved during normalization |
| `external_id` | TEXT | Source's own id |
| `url` | TEXT NOT NULL | |
| `title`, `normalized_title` | TEXT | |
| `seniority` | TEXT | `junior`/`mid`/`senior`/`lead`/`unknown` |
| `location_raw`, `location_city`, `location_state`, `location_country` | TEXT | |
| `is_remote`, `is_hybrid` | BOOLEAN | |
| `employment_type` | TEXT | |
| `salary_min`, `salary_max`, `salary_currency`, `salary_period` | — | Nullable; parsed, never guessed |
| `description_raw` | TEXT | Sanitized HTML→text |
| `description_hash` | TEXT | For dedupe |
| `requirements` | JSONB | Extracted structured requirements (AGENT_DESIGN §3.3 A1) |
| `posted_at`, `expires_at`, `first_seen_at`, `last_seen_at` | TIMESTAMPTZ | |
| `status` | job_status NOT NULL DEFAULT 'discovered' | |
| `discard_reason` | TEXT | |
| `duplicate_of_id` | UUID FK → jobs NULL | Self-reference |
| `priority` | INTEGER NOT NULL DEFAULT 0 | Manual queue override |
| `created_at`, `updated_at` | TIMESTAMPTZ | |

```sql
CREATE UNIQUE INDEX uq_jobs_source_external ON jobs(source_id, external_id)
  WHERE external_id IS NOT NULL;
CREATE INDEX idx_jobs_status_priority ON jobs(status, priority DESC, posted_at DESC);
CREATE INDEX idx_jobs_dedupe ON jobs(company_id, normalized_title, description_hash);
CREATE INDEX idx_jobs_title_trgm ON jobs USING gin (normalized_title gin_trgm_ops);
```

### 5.5 `job_raw_snapshots`

Immutable capture of what was actually seen, for audit and re-parsing: `id`, `job_id`, `captured_at`, `content` (TEXT), `content_type`, `http_status`, `source_url`. Retained 90 days (SECURITY.md §9).

### 5.6 `job_evaluations` — immutable

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `job_id` | UUID FK → jobs ON DELETE CASCADE | |
| `profile_version` | INTEGER NOT NULL | Which profile state was evaluated |
| `score` | NUMERIC(5,2) NOT NULL | 0–100 |
| `recommendation` | recommendation NOT NULL | |
| `match_matrix` | JSONB NOT NULL | requirement → `{status, fact_ids[], note}` |
| `matched_requirements`, `missing_requirements` | JSONB | Denormalized for fast UI |
| `gaps` | TEXT[] | Named honestly |
| `rationale` | TEXT | |
| `score_breakdown` | JSONB | Component weights, for explainability |
| `agent_run_id` | UUID FK → agent_runs | |
| `model`, `prompt_version` | TEXT | |
| `created_at` | TIMESTAMPTZ | |

No `updated_at` — evaluations are append-only. Re-evaluation creates a new row; the UI shows the latest and offers history.

---

## 6. Application domain

### 6.1 `applications`

```sql
CREATE TABLE applications (
  id                 UUID PRIMARY KEY,
  job_id             UUID NOT NULL REFERENCES jobs(id),
  profile_id         UUID NOT NULL REFERENCES profiles(id),
  evaluation_id      UUID REFERENCES job_evaluations(id),
  status             application_status NOT NULL DEFAULT 'created',
  outcome            application_outcome NOT NULL DEFAULT 'pending',
  resume_id          UUID REFERENCES resumes(id),
  cover_letter_id    UUID REFERENCES cover_letters(id),
  submission_method  submission_method,
  priority           INTEGER NOT NULL DEFAULT 0,
  approved_at        TIMESTAMPTZ,
  approved_by        UUID REFERENCES users(id),
  submitted_at       TIMESTAMPTZ,
  outcome_updated_at TIMESTAMPTZ,
  error_reason       TEXT,
  abandon_reason     TEXT,
  notes              TEXT,
  version            INTEGER NOT NULL DEFAULT 1,
  created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),

  -- structural enforcement of human-in-the-loop
  CONSTRAINT chk_approved_requires_human CHECK (
    status NOT IN ('approved','submission_ready','submitting','submitted','manual_handoff')
    OR (approved_by IS NOT NULL AND approved_at IS NOT NULL)
  ),
  CONSTRAINT chk_submitted_requires_timestamp CHECK (
    status <> 'submitted' OR submitted_at IS NOT NULL
  )
);

-- one live application per job
CREATE UNIQUE INDEX uq_application_per_job ON applications(job_id)
  WHERE status NOT IN ('abandoned','error','policy_blocked');

CREATE INDEX idx_applications_status ON applications(status, updated_at DESC);
CREATE INDEX idx_applications_outcome ON applications(outcome, submitted_at DESC);
```

`chk_approved_requires_human` is the most important constraint in the schema: **a bug in application code cannot produce a submitted application without a recorded human approver.** The database refuses it.

### 6.2 `resumes`

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `profile_id` | UUID FK → profiles | |
| `application_id` | UUID FK → applications NULL | NULL for the base resume |
| `kind` | TEXT NOT NULL | `base` \| `tailored` |
| `version` | INTEGER NOT NULL DEFAULT 1 | Increments on regeneration |
| `content` | JSONB NOT NULL | Structured resume: sections, bullets, ordering |
| `summary_text` | TEXT | |
| `diff_vs_base` | JSONB | Added / removed / reordered / rephrased |
| `verification_status` | TEXT NOT NULL DEFAULT 'pending' | `pending` \| `passed` \| `failed` |
| `verification_report` | JSONB | Per-claim verdicts and reasons |
| `agent_run_id` | UUID FK → agent_runs | |
| `model`, `prompt_version` | TEXT | |
| `created_at`, `updated_at` | TIMESTAMPTZ | |

`content` is structured rather than a text blob so the renderer, the diff view, and the claim table all operate on the same object and cannot drift.

### 6.3 `resume_claims` — traceability

```sql
CREATE TABLE resume_claims (
  id             UUID PRIMARY KEY,
  resume_id      UUID NOT NULL REFERENCES resumes(id) ON DELETE CASCADE,
  section        TEXT NOT NULL,        -- summary | experience | skills | projects | ...
  path           TEXT NOT NULL,        -- JSON pointer into resumes.content
  claim_text     TEXT NOT NULL,
  fact_ids       UUID[] NOT NULL,
  numerals_used  TEXT[] NOT NULL DEFAULT '{}',
  verdict        claim_verdict NOT NULL DEFAULT 'not_checked',
  verdict_reason TEXT,
  verified_at    TIMESTAMPTZ,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT chk_claim_has_facts CHECK (cardinality(fact_ids) > 0)
);
CREATE INDEX idx_claims_resume ON resume_claims(resume_id, verdict);
```

`chk_claim_has_facts` means an uncited claim cannot be persisted at all. Rendering queries `WHERE verdict = 'supported'`; anything else never reaches a PDF.

`cover_letters` mirrors `resumes` (same claim discipline, `cover_letter_claims` with identical shape).

### 6.4 `application_questions` / `application_answers`

**`application_questions`**: `id`, `application_id`, `external_field_id`, `question_text`, `input_type` (`text`/`textarea`/`select`/`multiselect`/`boolean`/`file`/`date`/`number`), `options` JSONB, `is_required`, `max_length`, `display_order`, `question_class` (question_class), `class_reason`, `created_at`.

**`application_answers`**:

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `question_id` | UUID FK UNIQUE | one live answer per question |
| `application_id` | UUID FK | denormalized for query |
| `value` | TEXT | |
| `value_structured` | JSONB | for select/multiselect |
| `source` | answer_source NOT NULL | `agent` \| `human` \| `derived` \| `blank` |
| `fact_ids` | UUID[] | citations when source = agent/derived |
| `confidence` | NUMERIC(4,3) | agent confidence |
| `requires_human` | BOOLEAN NOT NULL DEFAULT true | |
| `answered_by` | UUID FK → users NULL | |
| `edited_from_agent_value` | TEXT | preserves what the agent proposed, for eval feedback |
| `created_at`, `updated_at` | TIMESTAMPTZ | |

```sql
CONSTRAINT chk_sensitive_human_only CHECK (
  NOT (requires_human = true AND source = 'agent' AND answered_by IS NULL)
);
```

Together with the question classifier, this means a `sensitive` or `judgement` question cannot hold an agent-authored answer without a human's user id attached to it.

### 6.5 `approvals`

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `application_id` | UUID FK → applications ON DELETE CASCADE | |
| `kind` | approval_kind NOT NULL | |
| `status` | approval_status NOT NULL DEFAULT 'pending' | |
| `subject_snapshot` | JSONB NOT NULL | **Exactly what the reviewer was shown** |
| `guardrail_flags` | JSONB | Anything the stack flagged |
| `decision_note` | TEXT | |
| `changes_requested` | TEXT | Instructions for rework |
| `edits_applied` | JSONB | What the reviewer changed |
| `requested_at` | TIMESTAMPTZ NOT NULL | |
| `decided_at` | TIMESTAMPTZ | |
| `decided_by` | UUID FK → users | |
| `created_at`, `updated_at` | TIMESTAMPTZ | |

```sql
CONSTRAINT chk_decided_has_actor CHECK (
  status = 'pending' OR status = 'superseded'
  OR (decided_by IS NOT NULL AND decided_at IS NOT NULL)
);
CREATE UNIQUE INDEX uq_one_pending_approval
  ON approvals(application_id, kind) WHERE status = 'pending';
```

`subject_snapshot` is what makes the audit trail meaningful: six months later it is possible to see not just that an application was approved, but the exact resume text and answers the human saw when approving it.

### 6.6 `submissions`

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `application_id` | UUID FK | |
| `attempt` | INTEGER NOT NULL | |
| `method` | submission_method NOT NULL | |
| `target_domain` | TEXT NOT NULL | |
| `policy_id` | UUID FK → site_policies | Policy in force at submit time |
| `result` | submission_result NOT NULL DEFAULT 'pending' | |
| `http_status` | INTEGER | |
| `external_reference` | TEXT | Confirmation id if provided |
| `failure_reason` | TEXT | |
| `blocked_reason` | TEXT | e.g. `captcha_encountered`, `policy_forbids_automation` |
| `packet_artifact_id` | UUID FK → artifacts | For human_handoff |
| `started_at`, `finished_at` | TIMESTAMPTZ | |
| `created_at` | TIMESTAMPTZ | |

`blocked_reason` deliberately records CAPTCHA and bot-detection encounters as terminal facts. They are metrics to review, never inputs to a workaround.

### 6.7 `application_status_history`

Append-only: `id`, `application_id`, `from_status`, `to_status`, `actor_type` (`agent`/`human`/`system`), `actor_id`, `event_id`, `reason`, `created_at`. Written by the state machine only; no UPDATE or DELETE grants for anyone.

### 6.8 `artifacts`

`id`, `kind` (`resume_pdf`/`resume_docx`/`cover_letter_pdf`/`handoff_packet`/`evidence`), `application_id` NULL, `resume_id` NULL, `storage_key`, `bucket`, `content_type`, `size_bytes`, `sha256`, `created_at`, `expires_at`. Files live in MinIO/S3; only metadata is in Postgres.

---

## 7. Operations tables

### 7.1 `events` — outbox and audit log

```sql
CREATE TABLE events (
  id              UUID PRIMARY KEY,
  type            TEXT NOT NULL,             -- see EVENTS.md
  version         INTEGER NOT NULL DEFAULT 1,
  aggregate_type  TEXT NOT NULL,             -- job | application | resume | approval | system
  aggregate_id    UUID,
  correlation_id  UUID NOT NULL,             -- ties a whole pipeline together
  causation_id    UUID,                      -- the event that caused this one
  actor_type      TEXT NOT NULL,             -- agent | human | system
  actor_id        TEXT,
  trace_id        TEXT,
  payload         JSONB NOT NULL,
  occurred_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  published       BOOLEAN NOT NULL DEFAULT false,
  published_at    TIMESTAMPTZ
);
CREATE INDEX idx_events_unpublished ON events(occurred_at) WHERE published = false;
CREATE INDEX idx_events_aggregate ON events(aggregate_type, aggregate_id, occurred_at DESC);
CREATE INDEX idx_events_correlation ON events(correlation_id, occurred_at);
CREATE INDEX idx_events_type_time ON events(type, occurred_at DESC);
```

No UPDATE grant except the outbox relay's `published` flag; no DELETE grant for any role.

### 7.2 `processed_events` — idempotency

`event_id` + `consumer_group` composite PK, `processed_at`, `result`. Every handler checks this first. 30-day retention.

### 7.3 `agent_runs` / `agent_run_steps` / `llm_calls`

**`agent_runs`**: `id`, `agent` (agent_name), `pipeline` (`evaluation`/`tailoring`/`packet`/`submission`/`digest`), `trigger_event_id`, `aggregate_type`, `aggregate_id`, `correlation_id`, `status` (run_status), `steps_total`, `steps_completed`, `input_tokens`, `output_tokens`, `cost_usd` NUMERIC(12,4), `duration_ms`, `error_type`, `error_message`, `prompt_version`, `started_at`, `finished_at`.

**`agent_run_steps`**: `id`, `run_id`, `step_index`, `step_name`, `status`, `input_digest`, `output` JSONB, `guardrail_results` JSONB, `duration_ms`, `started_at`, `finished_at`. Enables step-level resume after a worker restart.

**`llm_calls`**: `id`, `run_id`, `step_id`, `model`, `input_tokens`, `output_tokens`, `cost_usd`, `latency_ms`, `stop_reason`, `prompt_hash`, `prompt_redacted` TEXT, `response_redacted` TEXT, `created_at`. Redacted content only, 30-day retention.

### 7.4 `daily_quotas`

`id`, `date` (DATE), `quota_type` (`applications`/`llm_cost`/`evaluations`/`submissions`), `limit_value`, `used_value`, `updated_at`. `UNIQUE(date, quota_type)`. Redis holds the fast counter; this table is the durable ledger and the reconciliation target. The application-creation path takes a row lock here, so 100/day is enforced even under concurrent workers.

### 7.5 `metrics_snapshots`

`id`, `grain` (`5min`/`hour`/`day`), `bucket_start`, `metrics` JSONB, `created_at`, `UNIQUE(grain, bucket_start)`. Written by Agent 3 only.

### 7.6 `alerts`

`id`, `kind`, `severity` (alert_severity), `title`, `detail` JSONB, `aggregate_type`, `aggregate_id`, `status` (`open`/`acknowledged`/`resolved`), `auto_paused` BOOLEAN, `acknowledged_by`, `acknowledged_at`, `resolved_at`, `created_at`.

`auto_paused = true` on a critical fabrication alert corresponds to the pipeline pause in ARCHITECTURE.md §14.2.

### 7.7 `notifications`

`id`, `channel` (`in_app`/`email`), `kind`, `title`, `body`, `payload` JSONB, `read_at`, `dispatched_at`, `created_at`.

### 7.8 `audit_logs`

`id`, `actor_type`, `actor_id`, `action`, `resource_type`, `resource_id`, `before` JSONB, `after` JSONB, `ip_address` INET, `user_agent`, `created_at`. Populated by a database trigger on `profiles`, `profile_facts`, `site_policies`, `settings`, `applications`, `approvals` — so it captures changes even if they bypass the application layer. Insert-only for every role.

### 7.9 `settings`

`key` PK, `value` JSONB, `description`, `updated_by`, `updated_at`. Holds: fit thresholds, daily quota, model per pipeline, approval mode, notification prefs, retention windows, `submission_enabled` master switch. `GRANT SELECT` only to `app_agent`.

---

## 8. Database roles

```sql
CREATE ROLE app_api  LOGIN;   -- FastAPI: full CRUD on operational tables
CREATE ROLE app_agent LOGIN;  -- agent workers
CREATE ROLE app_monitor LOGIN;-- Agent 3
CREATE ROLE app_migrate LOGIN;-- Alembic only, DDL

-- app_agent
GRANT SELECT ON ALL TABLES IN SCHEMA public TO app_agent;
GRANT INSERT, UPDATE ON jobs, job_evaluations, resumes, resume_claims,
      cover_letters, application_questions, application_answers,
      applications, submissions, events, agent_runs, agent_run_steps,
      llm_calls, processed_events TO app_agent;
REVOKE INSERT, UPDATE, DELETE ON profile_facts, profiles, experiences,
      educations, certifications, skills, projects, site_policies,
      settings, users, approvals FROM app_agent;

-- app_monitor
GRANT SELECT ON ALL TABLES IN SCHEMA public TO app_monitor;
GRANT INSERT, UPDATE ON metrics_snapshots, alerts, notifications,
      processed_events, events TO app_monitor;

-- nobody deletes events or audit_logs
REVOKE DELETE ON events, audit_logs, application_status_history FROM PUBLIC;
```

The three hardest safety requirements — no invented facts, no self-approval, no unauditable action — are each backed by a grant, not only by code.

---

## 9. Indexing summary

| Query pattern | Index |
|---|---|
| Next-up queue | `idx_jobs_status_priority` |
| Dedupe on ingest | `idx_jobs_dedupe`, `uq_jobs_source_external` |
| Fuzzy title search | `idx_jobs_title_trgm` |
| Fact retrieval for tailoring | `idx_facts_embedding` (HNSW) + `idx_facts_text_trgm` |
| Applications board | `idx_applications_status` |
| Outcome reporting | `idx_applications_outcome` |
| Approvals inbox | `uq_one_pending_approval` + partial index on `status='pending'` |
| Outbox relay | `idx_events_unpublished` (partial — stays tiny) |
| Audit / timeline | `idx_events_aggregate`, `idx_events_correlation` |

At 5,000 jobs/day and 100 applications/day, total data volume after a year is well under 10 GB. No partitioning is needed; `events` and `job_raw_snapshots` get monthly range partitioning only if volume unexpectedly grows.

---

## 10. Data retention

| Data | Retention | Mechanism |
|---|---|---|
| `job_raw_snapshots.content` | 90 days | Nightly job nulls content, keeps metadata |
| `llm_calls.prompt_redacted` / `response_redacted` | 30 days | Nightly purge; token/cost rows kept forever |
| `processed_events` | 30 days | Nightly purge |
| `events` | Indefinite | Audit record |
| `applications`, `resumes`, `approvals`, `audit_logs` | Indefinite | The user's own record |
| `artifacts` | 1 year, then lifecycle to S3 Glacier | S3 lifecycle rule |

Full export (`scripts/export.py`) and full purge (`scripts/purge.py`) both exist from Phase 1, because it's the user's own personal data and they should be able to take it or destroy it at will.

---

## 11. Consistency with other documents

- Status enums in §3 are the mirror of ARCHITECTURE.md §7. Any change updates both.
- `events.type` values are constrained to the catalogue in EVENTS.md §3 (validated in application code and by a CI contract test, not by a DB `CHECK`, so adding an event type isn't a migration).
- Grants in §8 implement the capability matrix in AGENT_DESIGN.md §7.
- Retention in §10 matches SECURITY.md §9.

---

*End of DATABASE_DESIGN.md — v1.0*
