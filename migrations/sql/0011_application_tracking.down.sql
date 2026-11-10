DROP INDEX IF EXISTS idx_applications_follow_up;
ALTER TABLE applications DROP CONSTRAINT IF EXISTS ck_applications_outcome_requires_applied;
ALTER TABLE applications DROP COLUMN IF EXISTS follow_up_on;
ALTER TABLE applications DROP COLUMN IF EXISTS applied_on;
