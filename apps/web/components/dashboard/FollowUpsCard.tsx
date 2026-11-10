"use client";

/**
 * M19: follow-ups due today or overdue, plus the next seven days.
 *
 * Dates come from the owner's own tracking records; "today" is the
 * browser's local date, sent to the server so the cut-off matches what the
 * owner sees. Closed outcomes (accepted, rejected, withdrawn) never appear.
 */

import Link from "next/link";
import { useEffect, useState } from "react";
import { SectionHeader } from "@/components/SectionHeader";
import { OUTCOME_LABEL, dueText, localToday, tracking, type FollowUp } from "@/lib/tracking";

export const LOOK_AHEAD_DAYS = 7;

export function FollowUpsCard() {
  const [rows, setRows] = useState<FollowUp[] | null | undefined>(undefined);

  useEffect(() => {
    let cancelled = false;
    tracking
      .followUps(localToday(), LOOK_AHEAD_DAYS)
      .then((r) => !cancelled && setRows(r))
      .catch(() => !cancelled && setRows(null));
    return () => {
      cancelled = true;
    };
  }, []);

  const due = rows?.filter((r) => r.days_overdue >= 0) ?? [];
  const upcoming = rows?.filter((r) => r.days_overdue < 0) ?? [];

  return (
    <section aria-labelledby="follow-ups-heading">
      <SectionHeader
        id="follow-ups-heading"
        title="Follow-ups"
        description="Reminders you set on your applications."
      />
      <div className="rounded-card border border-edge bg-surface p-5 shadow-raised" data-testid="follow-ups">
        {rows === undefined ? (
          <p className="text-sm text-ink-soft">Loading follow-ups…</p>
        ) : rows === null ? (
          <p className="text-sm text-danger">Follow-ups are unavailable right now.</p>
        ) : rows.length === 0 ? (
          <p className="text-sm text-ink-soft">No follow-ups due in the next {LOOK_AHEAD_DAYS} days.</p>
        ) : (
          <div className="space-y-4">
            {[
              { label: `Due now (${due.length})`, items: due },
              { label: `Coming up (${upcoming.length})`, items: upcoming },
            ]
              .filter((group) => group.items.length > 0)
              .map((group) => (
                <div key={group.label}>
                  <h3 className="text-xs font-semibold uppercase tracking-wide text-ink-soft">{group.label}</h3>
                  <ul className="mt-2 space-y-2">
                    {group.items.map((row) => (
                      <li key={row.application_id}>
                        <Link
                          href={`/applications/${row.application_id}`}
                          className="flex min-h-11 flex-wrap items-center justify-between gap-2 rounded-chip border border-edge px-3 py-2 text-sm hover:border-control"
                        >
                          <span className="min-w-0">
                            <span className="font-medium text-ink">{row.job_title}</span>
                            <span className="text-ink-soft">
                              {row.company_name ? ` at ${row.company_name}` : ""} · {row.applied_on ? OUTCOME_LABEL[row.outcome] : "Not applied yet"}
                            </span>
                          </span>
                          <span className={`text-xs ${row.days_overdue > 0 ? "font-semibold text-attention" : "text-ink-soft"}`}>
                            {row.follow_up_on} · {dueText(row.days_overdue)}
                          </span>
                        </Link>
                      </li>
                    ))}
                  </ul>
                </div>
              ))}
          </div>
        )}
      </div>
    </section>
  );
}
