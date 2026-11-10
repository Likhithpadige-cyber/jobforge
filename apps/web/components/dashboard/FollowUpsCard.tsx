"use client";

/**
 * M19: follow-ups due today or overdue, plus the next seven days.
 *
 * Dates come from the owner's own tracking records; "today" is the
 * browser's local date, sent to the server so the cut-off matches what the
 * owner sees. Closed outcomes (accepted, rejected, withdrawn) never appear.
 * The owner can mark a follow-up done (clears the date) or snooze it a week
 * from here; both are the same owner-only tracking update as the workspace.
 */

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { useAuth } from "@/components/AuthGuard";
import { SectionHeader } from "@/components/SectionHeader";
import { apiErrorText } from "@/lib/api";
import { APPLIED_VIA_LABEL, OUTCOME_LABEL, addDays, dueText, localToday, tracking, type FollowUp, type TrackingChange } from "@/lib/tracking";

export const LOOK_AHEAD_DAYS = 7;
const ACTION = "min-h-11 rounded-full border border-edge bg-surface px-3 text-xs font-medium text-ink hover:border-control disabled:opacity-60";

export function FollowUpsCard() {
  const { user } = useAuth();
  const isOwner = user?.role === "owner";
  const [rows, setRows] = useState<FollowUp[] | null | undefined>(undefined);
  const [busy, setBusy] = useState<string | null>(null);
  const [problem, setProblem] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setRows(await tracking.followUps(localToday(), LOOK_AHEAD_DAYS));
    } catch {
      setRows(null);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function act(row: FollowUp, change: TrackingChange) {
    setBusy(row.application_id);
    setProblem(null);
    try {
      await tracking.update(row.application_id, change);
      await load();
    } catch (cause) {
      setProblem(apiErrorText(cause, "The follow-up could not be updated."));
    } finally {
      setBusy(null);
    }
  }

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
                      <li key={row.application_id} className="space-y-1">
                        <Link
                          href={`/applications/${row.application_id}`}
                          className="flex min-h-11 flex-wrap items-center justify-between gap-2 rounded-chip border border-edge px-3 py-2 text-sm hover:border-control"
                        >
                          <span className="min-w-0">
                            <span className="font-medium text-ink">{row.job_title}</span>
                            <span className="text-ink-soft">
                              {row.company_name ? ` at ${row.company_name}` : ""} · {row.applied_via ? `${OUTCOME_LABEL[row.outcome]}, ${APPLIED_VIA_LABEL[row.applied_via]}` : "Not applied yet"}
                            </span>
                          </span>
                          <span className={`text-xs ${row.days_overdue > 0 ? "font-semibold text-attention" : "text-ink-soft"}`}>
                            {row.follow_up_on} · {dueText(row.days_overdue)}
                          </span>
                        </Link>
                        {isOwner && (
                          <div className="flex flex-wrap gap-2 pl-1">
                            <button type="button" disabled={busy !== null} aria-label={`Mark follow-up done: ${row.job_title}`} className={ACTION} onClick={() => void act(row, { follow_up_on: null })}>
                              Done
                            </button>
                            <button type="button" disabled={busy !== null} aria-label={`Snooze one week: ${row.job_title}`} className={ACTION} onClick={() => void act(row, { follow_up_on: addDays(localToday(), 7) })}>
                              Snooze 1 week
                            </button>
                          </div>
                        )}
                      </li>
                    ))}
                  </ul>
                </div>
              ))}
          </div>
        )}
        {problem && <p role="alert" className="mt-3 text-sm text-danger">{problem}</p>}
      </div>
    </section>
  );
}
