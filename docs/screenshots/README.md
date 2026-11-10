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
| `approvals.png` | Approvals inbox: work waiting for a human decision |
| `cover-letter-review.png` | Cover-letter review: each paragraph with its cited-evidence verdict |
| `submission-readiness.png` | Submission readiness checklist, blocked by dry-run mode |

Every image shows the seed data as served, with nothing filtered or edited, so
the totals agree across pages: 2 applications, 2 items awaiting review.

## Added 2026-11-10

`application-tracking.png` and `settings-sessions.png` were captured the same
way (Playwright, Chromium, 1280×800, reduced motion; Next.js development
overlay hidden) from a throwaway copy of the fictional demo, signed in as a
run-unique synthetic E2E owner. The tracking values on the seeded fictional
"Cloud Engineer" application (dates and the note "Fictional demo: phone
screen booked.") were entered for the capture. The earlier `applications.png`
was retired because it showed a note that outcomes were not yet available.
