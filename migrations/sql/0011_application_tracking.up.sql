-- M19: owner-recorded application tracking, orthogonal to the pipeline
-- status (ARCHITECTURE.md §7.3). `applied_on` is the date the owner says the
-- application was sent - by JobForge or by hand - and is distinct from
-- `submitted_at`, which only the approved submission pipeline writes.
-- `follow_up_on` is a reminder date the owner sets. Neither column is in
-- app_agent's column-level UPDATE grant (0003): only the API's human path
-- writes them. Changes are captured by trg_applications_audit.
ALTER TABLE applications ADD COLUMN applied_on DATE;
ALTER TABLE applications ADD COLUMN follow_up_on DATE;

-- An outcome beyond `pending` (interviewing, offer, rejected, ...) only
-- makes sense for something that was applied to. Nothing wrote `outcome`
-- before M19, so every existing row is `pending` and this validates.
ALTER TABLE applications ADD CONSTRAINT ck_applications_outcome_requires_applied
  CHECK (outcome = 'pending' OR applied_on IS NOT NULL);

-- The due-follow-ups dashboard reads only rows with a reminder set.
CREATE INDEX idx_applications_follow_up ON applications (follow_up_on)
  WHERE follow_up_on IS NOT NULL;
