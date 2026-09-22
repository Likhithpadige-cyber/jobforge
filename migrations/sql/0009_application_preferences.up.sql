-- M9: candidate employment-type preference, and an evidence-integrity guard
-- for agent-authored application answers.
--
-- `accepted_employment_types` is nullable and carries no default: NULL means
-- "no preference set", not "full-time only". A candidate who never touches
-- this setting sees no employment-type filtering at all, which is what keeps
-- "advance only explicitly full-time postings" a per-candidate configuration
-- rather than a rule this migration bakes into every JobForge deployment.
ALTER TABLE profiles ADD COLUMN accepted_employment_types TEXT[];

-- Mirrors `ck_resume_claims_claim_has_facts`, scoped to `source = 'agent'`
-- only. An agent-authored answer must cite at least one profile_facts row,
-- the same discipline resume_claims already enforces for generated resume
-- text. `derived` answers are deliberately exempt: M9 uses that source for
-- deterministic, code-computed answers grounded in a profile column or a
-- candidate preference (e.g. accepted_employment_types) rather than a
-- profile_facts citation, and forcing fact_ids there would either require
-- fabricating a fact for something that is not one, or silently misclassify
-- a legitimate derivation as uncited.
ALTER TABLE application_answers
    ADD CONSTRAINT ck_application_answers_agent_requires_facts CHECK (
        source <> 'agent' OR cardinality(fact_ids) > 0
    );
