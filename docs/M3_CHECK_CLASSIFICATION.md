# M3 remaining lint/type findings — captured before scoped cleanup

Scope: the 13 Ruff findings and 22 mypy errors exposed by the required M1–M3
verification gates. No new rule suppressions, schema changes, or UI changes.
Each correction must preserve production behavior and existing security assertions.

## Ruff: 13 findings

| File / original line | Count | Root cause / individually selected correction |
| --- | ---: | --- |
| apps/api/schemas/profile.py:51 | 1 | SIM102: combine nested salary comparisons with identical short-circuit order. |
| packages/agents/resume_optimizer/requirements.py:71 | 1 | RUF001: intentional en dash in regex; use an explicit Unicode regex escape, retaining en-dash matching. |
| packages/services/ingestion/normalize.py:39,80,156,157,158,213 | 6 | RUF001: intentional en dashes in regexes and strip strings; spell the same character as Unicode escapes, never replace it with a hyphen. |
| packages/shared/types.py:5 | 1 | F401: remove obsolete TypeAlias import left after prior syntax cleanup. |
| tests/integration/test_profile_crud.py:66 | 1 | B017: existing rejection assertion catches arbitrary exceptions; verify the actual constraint exception before narrowing to IntegrityError. Preserve rejection assertion. |
| tests/integration/test_security_invariants.py:8 | 1 | I001: move contextlib to standard-library imports. |
| tests/integration/test_security_invariants.py:196,246 | 2 | SIM105: use suppress for the same two exception classes; retain outcome assertions and explanatory comments. |

## Mypy: 22 errors in 13 files

| File / original line | Count | Root cause / individually selected correction |
| --- | ---: | --- |
| packages/services/ingestion/connectors/manual.py:56 | 1 | Parser tuple contains unannotated lambdas; give the tuple its Callable[[str], datetime] element type. Keep parser order and bodies. |
| packages/core/auth/sessions.py:100,125,175 | 3 | Redis parameter annotations missing; describe only the asynchronous session-store operations used. |
| packages/core/cache/rate_limit.py:108,152 | 2 | Redis parameter annotations missing; describe the pipeline interface without changing counters, expiry, or fail-closed exception handling. |
| packages/shared/logging.py:132 | 1 | structlog dynamic factory returns Any; type the existing configured logging boundary, preserving processors and redaction. |
| packages/core/repositories/base.py:47 | 1 | Untyped _base_select erases ModelT; annotate its Select[tuple[ModelT]] return. |
| packages/core/repositories/facts.py:73,154 | 2 | Bare structured dict plus untyped raw SQL scalar; add JSON mapping parameters and express stored function INTEGER return contract. |
| packages/core/events/outbox.py:131 | 1 | Declarative __table__ is statically FromClause but this mapped model uses a concrete Table; annotate that verified metadata boundary, preserving identical INSERT target and statement. |
| packages/agents/resume_optimizer/prefilter.py:294 | 1 | sum default integer zero makes division inference include float; preserve the existing nonempty Decimal sums with an explicit Decimal zero. |
| packages/agents/llm/runtime.py:128 | 1 | HTTP JSON deserialization is Any while Transport already declares a JSON object; annotate this provider-envelope boundary. Preserve downstream structured validation and error behavior. |
| apps/api/routers/health.py:58,59,72 | 3 | Stale arg-type ignores; remove only these comments. |
| packages/services/ingestion/pipeline.py:160 | 2 | ORM descriptors fail structural protocol matching even with read-only properties. Retain the original protocol and use a documented cast only where verified Job instance fields enter the pure comparator. No row copying or concrete model changes. |
| packages/agents/resume_optimizer/persistence.py:163 | 3 | Matrix JSON dict[str, object] loses the requirements array shape; unused ignore, noniterable object, and cascading sum inference. Express the producer's existing array contract at the serialization boundary. |
| apps/api/routers/jobs.py:73 | 1 | Stale arg-type ignore; remove only the comment. |

Potential casts require verified boundary evidence, not merely a mypy complaint.
No runtime validation, provider behavior, rate-limit policy, database query target,
or public request/response shape is to be changed by this cleanup.


## Final implementation notes

- All fixes were individually targeted; no repository-wide automatic transform
  was run in this cleanup turn.
- Redis protocols use positional inputs and Awaitable return types, matching the
  installed redis-py API and preserving shared FakeRedis behavior.
- The logger factory remains structlog.get_logger. A quoted cast preserves the
  pre-existing wrapper contract without adding a runtime type lookup. Automatic
  review rejected earlier wrapper/type-lookup proposals; neither proposal ran.
- The fact-usage boundary retains the existing INTEGER contract for existing facts
  (migration 0002), and the outbox retains the identical concrete Table target.
- Provider JSON and evaluation-matrix casts annotate existing serialized envelope
  shapes; validation order and exception behavior remain unchanged.
- Decimal sums are nonempty (three mandatory components); an explicit Decimal zero
  yields the same arithmetic while removing spurious float inference.
- The certification test now requires IntegrityError naming its existing CHECK
  constraint, strengthening its original rejection assertion.
- No new noqa, type-ignore, schema changes, or UI changes were introduced.
- make lint: zero findings. make typecheck: zero errors, 110 files checked.
