-- M8: bind a rendered artifact to the exact content a human approved.
--
-- Additive only, and one ADD COLUMN per statement rather than a single
-- multi-clause ALTER. Postgres is happy either way for nullable columns, but
-- scripts/verify_schema.py reads this file with a per-statement regex, and a
-- schema check that silently skips columns is worse than a slightly longer
-- migration.
--
-- The columns are nullable at the table level because `artifacts` also holds
-- evidence uploads and handoff packets, which have no resume, no approval and
-- no template. The CHECK narrows them to NOT NULL for the two rendered-resume
-- kinds, so the contract binds exactly where it applies instead of being
-- weakened for everyone.
ALTER TABLE artifacts ADD COLUMN resume_version INTEGER;
ALTER TABLE artifacts ADD COLUMN approval_id UUID;
ALTER TABLE artifacts ADD COLUMN template_version TEXT;

ALTER TABLE artifacts
    ADD CONSTRAINT fk_artifacts_approval_id_approvals FOREIGN KEY (approval_id)
      REFERENCES approvals (id) ON DELETE SET NULL;

ALTER TABLE artifacts
    ADD CONSTRAINT ck_artifacts_render_binding CHECK (
        kind NOT IN ('resume_pdf', 'resume_docx')
        OR (
            resume_id IS NOT NULL
            AND resume_version IS NOT NULL
            AND approval_id IS NOT NULL
            AND template_version IS NOT NULL
            AND content_type IS NOT NULL
            AND size_bytes IS NOT NULL
            AND sha256 IS NOT NULL
        )
    );

-- The idempotency key. One rendering per (resume, format, template): a
-- replayed resume.render.requested cannot create a second row, and a template
-- change produces a new artifact rather than silently replacing the one a
-- human already downloaded.
--
-- Partial, because the other artifact kinds legitimately have no template and
-- would otherwise collide with each other on (NULL, kind, NULL).
CREATE UNIQUE INDEX uq_artifacts_resume_render
    ON artifacts (resume_id, kind, template_version)
    WHERE resume_id IS NOT NULL AND template_version IS NOT NULL;

-- M8 workers persist metadata. SELECT is already granted by 0003; add only
-- the missing capability, preserving API/owner grants and managed-role setups.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_agent') THEN
        GRANT INSERT ON artifacts TO app_agent;
    END IF;
END;
$$;
