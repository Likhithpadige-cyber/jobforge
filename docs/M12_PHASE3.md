# M12 Phase 3: operator UI

Implementation for review. Baseline: `646ada9` (M12 Phase 2 changelog).
No migration, no new event type, no grant change.

## Backend additions (owner only, additive)

`apps/api/routers/alerts.py`, behind the existing session, CSRF and
owner-role router dependencies.

- `GET /api/v1/alerts/generation-pause` adds `unresolved_auto_pause_count`
  (`packages/core/generation_safety.unresolved_auto_pause_count`). The pause is
  still computed only from unresolved `auto_paused` rows.
- `GET /api/v1/alerts` adds query parameters (all validated, 422 otherwise):

  | Parameter | Values |
  |---|---|
  | `status` | `open` (default; includes acknowledged), `resolved`, `all` |
  | `kind` | `^[a-z_]{1,64}$` |
  | `severity` | an alert severity |
  | `auto_paused` | `true` / `false` |
  | `limit` | 1–200, default 50 |
  | `offset` | 0–100000 |

  Results are newest first. The open list is now paginated; before this
  phase it returned every open alert.
- Every alert response (list, detail, resolve) adds:
  - `title`: derived from `kind` on the server, never the stored column, so
    it is always one of a fixed set of strings.
  - `requires_human`: true for any `auto_paused` row and every kind without
    a verified-recovery rule. It describes policy; it does not change it.
  - `resolution`: `human`, `monitor` or `null`, from the existing resolution
    marker.
  - `application_id` / `job_id`: exposed only for those aggregate types,
    because the UI can navigate to them. Other aggregate identifiers stay
    hidden.

  Raw `detail`, the stored title and subjects are still never returned.

## Frontend

- `lib/api.ts`:
  - A 503 is now an error except for the readiness probe, which opts in
    explicitly.
  - A non-JSON body (for example a proxy error page) surfaces as `ApiError`
    instead of a parse exception.
- `lib/operations.ts`: the typed client for the APIs above, plus display
  helpers.
  - A metric the server did not report renders as "Not available", never 0.
  - Trend gaps stay gaps.
- `components/operations/OperationsProvider.tsx`: owner-only, server-
  authoritative state. It refreshes pause status and the unresolved alert
  count on mount, every 30 s, and on focus or visibility.
  - Nothing is set optimistically.
  - A failed refresh keeps the last confirmed pause and marks it stale.
  - For any other role nothing is fetched.
- `GenerationPauseBanner`: on every page while the server reports a pause,
  with the remaining-incident count. It links to `/alerts?pausing=1`, which
  uses the server-side `auto_paused` filter so no pausing incident is missed
  by pagination.
- `/alerts`: status tabs, server-side severity, kind and pausing filters,
  load-more pagination, and empty, error and unauthorized states.
- `/alerts/[id]`:
  - Detail with policy, timeline, resolution source, and links to the
    application or job when exposed.
  - `ResolveAlertDialog` requires an explicit "I have reviewed" confirmation
    and states the effect on generation before the click.
  - After success it re-reads the alert and the pause status from the API.
  - A failed resolution changes nothing.
- `/monitoring`: the latest snapshot and two-hour sparklines (inline SVG, no
  chart dependency), covering:
  - worker health, consumer lag, dead letters, the submission failure
    window, stalled applications by status, pending approvals and
    unresolved alerts by kind;
  - a stale-snapshot warning after 15 minutes without an evaluation, since
    the monitor cannot report its own absence;
  - a Redis-unavailable notice;
  - "not run yet" and empty states.
- Navigation: an owner-only "Operations" sidebar section (Monitoring, Alerts).
  The header bell now shows the real unresolved alert count for owners; the
  former hard-coded "4 items" bell is gone, and reviewers see no bell.

Views use the existing tokens, `StatusBadge`, `EmptyState` and skeletons,
44 px touch targets, labelled controls, a focus-trapped modal dialog with
Escape, live regions for status changes, and tables with header scopes.

## Tests

- Backend `tests/integration/test_operator_alert_api.py`:
  - fixed titles and no leakage of detail, the stored title or the subject;
  - navigable references only;
  - `requires_human` against the resolution policy;
  - resolution source;
  - history filters and pagination, and invalid filters rejected;
  - the pause count across a two-incident resolution;
  - reviewer 403 and anonymous 401;
  - CSRF still required.

  The Phase 1 pause-contract assertions were extended for the new field.
- Frontend `__tests__/operations-lib.test.ts` and
  `__tests__/operations-ui.test.tsx`:
  - missing metrics never become zero;
  - the 503 and non-JSON handling;
  - the CSRF-protected resolve request;
  - banner visibility from server state, and the stale-status warning after
    a failed refresh;
  - no optimistic clearing while a resolution is in flight;
  - one-of-two and final resolution follow the server;
  - failed resolution changes nothing;
  - reviewer gating with no owner calls;
  - list, detail, monitoring, empty, error, not-found and unauthorized
    states;
  - the bell and navigation;
  - a guard that operator views never import demo data.

## Known limitations

- Polling every 30 s rather than server push; the banner can lag a new pause
  by up to one interval (it re-checks on focus).
- Rendering was verified by tests and the production build, not by a live
  browser session against real data (see the Phase 3 report).
