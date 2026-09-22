# Offline grounding evaluations (M6–M7)

Run `make test-evals`. The command runs without PostgreSQL, Redis, API keys or
provider money. It writes `.eval-results/offline.json` (ignored by Git), reports
per-category and per-mechanism counts, hashes the corpus and current prompts, and
returns nonzero for any failure, skip, collection error or missing corpus case.
The suite also runs in ordinary unit/security tests. CI publishes the JSON report.

`grounding_cases.json` is a versioned, hand-authored synthetic corpus: 36 cases
across 14 categories. M7 adds supported/missing/fabricated impact metrics, a
job-instruction attack and an invented responsibility. Never replace it with private user profiles or live logs.
It includes supported controls as well as adversarial outputs; rejecting every
claim fails the suite. Edit cases and expected outcomes explicitly during review.

The measured layers are separate:

- Deterministic output guards reject fabricated numerals/dates, unverified or
  foreign citations, missing citations, sensitive output and malformed JSON.
- Verifier replay exercises the actual B5 client, verdict enforcement and audit
  path for invented experience, employers, certifications, education and skills,
  plus supported counterparts. These replies are **scripted fixtures, not captured
  live judgments**. They prove negative verdicts block and are audited; they do
  not establish how often a real model will correctly identify a fabrication.
- Prompt cases check scanned hostile input remains inside the data boundary and
  cannot alter the system prompt; this does not prove every live prompt attack fails.
- M5 evidence cases verify missing requirements/support stay unresolved without
  a numeric score. Rationale cases verify cited counts, repeatability and no input
  mutation. Authorization facts are excluded from selection prompts.
- Harness checks verify malformed verifier output fails, missing replay cannot
  invoke a provider, and metrics cannot pass with missing/failed corpus cases.

Network connect operations are prohibited during these tests. Every verifier client
is explicitly replay mode, has no API key, and receives a transport that raises if
called. Test cassettes are generated from scripted responses in temporary folders,
bound to the exact current request digest; no live cassette is created or loaded.

## Reading results

A full pass means all curated guard/enforcement expectations held. It is not a
statistical estimate of model fabrication rate, and it is not an end-to-end test
of all resume generation or third-party submission. Per-mechanism reporting keeps
scripted-verifier results separate from deterministic rejection measurements.
The broader existing integration/security suite covers durable audit and storage.

## Optional live evaluation boundary

This command has no live mode, and no environment variable can turn it into one.
The existing `eval` marker remains reserved for explicitly authorized paid work;
these offline tests use `unit` and `safety`. A future live study requires a named
provider/model, approved spend cap, synthetic inputs, independent human semantic
labels, prompt/version pinning and separate reporting. No live semantic accuracy
claim or live test result is made by M6.
