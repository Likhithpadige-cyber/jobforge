-- Serialize the guard with writers; never delete or fabricate historical results.
LOCK TABLE job_evaluations IN ACCESS EXCLUSIVE MODE;
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM job_evaluations WHERE assessment_status <> 'assessed') THEN
        RAISE EXCEPTION 'Cannot downgrade: unresolved evaluations require the assessment contract';
    END IF;
END;
$$;
ALTER TABLE job_evaluations
    DROP CONSTRAINT ck_job_evaluations_assessment_contract,
    ALTER COLUMN score SET NOT NULL,
    ALTER COLUMN recommendation SET NOT NULL,
    DROP COLUMN assessment_status;
