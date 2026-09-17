-- Roll back only the new guard and lookup index. Approval decisions themselves
-- remain in place, so downgrading and re-upgrading does not alter cycle history.
DROP TRIGGER IF EXISTS trg_approvals_resume_rework_cycles ON approvals;
DROP FUNCTION IF EXISTS jobforge_guard_resume_rework_cycles();
DROP INDEX IF EXISTS ix_approvals_resume_rework_cycles;
