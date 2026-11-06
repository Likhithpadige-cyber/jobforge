# Screenshots

Captured on 2026-11-06 with Playwright (Chromium, 1280×800, PNG, reduced
motion) from the production-mode build of the frontend, running against a
**freshly seeded, throwaway copy of the fictional demo**
([demo environment](../DEMO_ENVIRONMENT.md)) in dry-run mode, signed in as the
seeded synthetic reviewer (`demo.reviewer@example.com`). The copy had its own
database, ports and generated local credentials, and was used only for these
captures.
No real person, applicant record, employer, credential or token appears in any
image.

| File | Shows |
|---|---|
| `login.png` | Sign-in page (no credentials entered) |
| `dashboard.png` | Dashboard: next decision, workspace totals, application pipeline |
| `applications.png` | Applications list with status and verification |
| `approvals.png` | Approvals inbox: work waiting for a human decision |
| `cover-letter-review.png` | Cover-letter review: each paragraph with its cited-evidence verdict |
| `submission-readiness.png` | Submission readiness checklist, blocked by dry-run mode |

Every image shows the seed data as served, with nothing filtered or edited, so
the totals agree across pages: 2 applications, 2 items awaiting review.
