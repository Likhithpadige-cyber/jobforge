# Screenshots

Captured on 2026-11-06 with Playwright (Chromium, 1280×800, PNG) from the
production-mode build of the frontend, running against the **fictional demo
dataset** ([demo environment](../DEMO_ENVIRONMENT.md)) in dry-run mode, signed
in as the seeded synthetic reviewer (`demo.reviewer@example.com`). No real
person, applicant record, employer, credential or token appears in any image.

| File | Shows |
|---|---|
| `login.png` | Sign-in page (no credentials entered) |
| `dashboard.png` | Dashboard: next decision, workspace totals, application pipeline |
| `applications.png` | Applications list with status and verification |
| `approvals.png` | Approvals inbox: work waiting for a human decision |
| `cover-letter-review.png` | Cover-letter review: each paragraph with its cited-evidence verdict |
| `submission-readiness.png` | Submission readiness checklist, blocked by dry-run mode |

**Disclosure.** The demo database also holds rows created by automated browser
tests (job titles beginning `E2E `). For the Applications and Approvals list
captures, those test rows were hidden from the browser's copy of the list
responses so the two seeded examples are visible; nothing else was altered and
nothing was written to the demo. The dashboard is unfiltered, so its totals
include the test rows.
