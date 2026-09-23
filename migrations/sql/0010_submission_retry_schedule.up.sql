-- M11 Phase 1: the one schema requirement the retry-scheduling design
-- identified. A future scheduler poll step (M11 Phase 3, not built yet)
-- will read this column to find attempts due for an automatic retry;
-- nothing in Phase 1 reads or writes it yet. Nullable, no default: NULL
-- means "no retry is scheduled for this attempt", which is every row's
-- state until Phase 3 exists to set it.
ALTER TABLE submissions ADD COLUMN next_attempt_after TIMESTAMPTZ;
