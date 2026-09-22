-- Refuse to drop the binding while any rendered resume depends on it.
-- Downgrading would leave artifact rows that can no longer say which version
-- or whose approval produced them, which is the provenance this migration
-- exists to record.
LOCK TABLE artifacts IN ACCESS EXCLUSIVE MODE;
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM artifacts WHERE kind IN ('resume_pdf', 'resume_docx')) THEN
        RAISE EXCEPTION 'Cannot downgrade: rendered resume artifacts require the render binding';
    END IF;
END;
$$;
DROP INDEX IF EXISTS uq_artifacts_resume_render;
ALTER TABLE artifacts
    DROP CONSTRAINT ck_artifacts_render_binding,
    DROP CONSTRAINT fk_artifacts_approval_id_approvals,
    DROP COLUMN template_version,
    DROP COLUMN approval_id,
    DROP COLUMN resume_version;

-- Only after the refusal guard and schema reversal succeed: restore 0003's
-- no-INSERT worker boundary, leaving existing SELECT and other roles alone.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_agent') THEN
        REVOKE INSERT ON artifacts FROM app_agent;
    END IF;
END;
$$;
