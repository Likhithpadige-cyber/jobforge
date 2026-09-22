-- A constant default backfills historical rows without UPDATEs to audit records.
ALTER TABLE job_evaluations
    ADD COLUMN assessment_status TEXT NOT NULL DEFAULT 'assessed',
    ALTER COLUMN score DROP NOT NULL,
    ALTER COLUMN recommendation DROP NOT NULL,
    ADD CONSTRAINT ck_job_evaluations_assessment_contract CHECK (
        (assessment_status = 'assessed' AND score IS NOT NULL AND recommendation IS NOT NULL)
        OR (assessment_status = 'insufficient_evidence' AND score IS NULL AND recommendation IS NULL)
    );
