"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { EmptyState } from "@/components/EmptyState";
import { StatusBadge } from "@/components/StatusBadge";
import type { ApprovalStatus } from "@/lib/api";
import { applicationStatus } from "@/lib/application-view";
import { formatTimestamp } from "@/lib/resumes";
import {
  APPROVAL_KIND_LABEL,
  approvalHref,
  review,
  reviewError,
  type ApprovalInboxItem,
  type ApprovalKind,
  type ApprovalQuery,
  type ReviewError,
} from "@/lib/review";
import { ReviewErrorState, ReviewLoading } from "./ReviewStates";

const PAGE_SIZE = 50;
const KIND_FILTERS: { value: ApprovalKind | ""; label: string }[] = [
  { value: "", label: "All" },
  { value: "resume", label: "Resumes" },
  { value: "cover_letter", label: "Cover letters" },
  { value: "answers", label: "Answers" },
  { value: "application", label: "Applications" },
];
const STATUS_FILTERS: { value: ApprovalStatus; label: string }[] = [
  { value: "pending", label: "Waiting on you" },
  { value: "approved", label: "Approved" },
  { value: "changes_requested", label: "Changes requested" },
  { value: "rejected", label: "Rejected" },
];

/** Every approval from the API, oldest waiting first. No counts are invented. */
export function ApprovalsInbox() {
  const [status, setStatus] = useState<ApprovalStatus>("pending");
  const [kind, setKind] = useState<ApprovalKind | "">("");
  const [rows, setRows] = useState<ApprovalInboxItem[]>([]);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<ReviewError | null>(null);
  const [attempt, setAttempt] = useState(0);

  const query = useCallback(
    (offset: number): ApprovalQuery => ({ status, kind: kind || undefined, limit: PAGE_SIZE, offset }),
    [status, kind],
  );

  useEffect(() => {
    let active = true;
    setLoading(true);
    setError(null);
    review
      .approvals(query(0))
      .then((result) => {
        if (!active) return;
        setRows(result);
        setHasMore(result.length === PAGE_SIZE);
      })
      .catch((cause) => active && setError(reviewError(cause)))
      .finally(() => active && setLoading(false));
    return () => {
      active = false;
    };
  }, [query, attempt]);

  async function loadMore() {
    setLoadingMore(true);
    try {
      const next = await review.approvals(query(rows.length));
      setRows((current) => [...current, ...next]);
      setHasMore(next.length === PAGE_SIZE);
    } catch (cause) {
      setError(reviewError(cause));
    } finally {
      setLoadingMore(false);
    }
  }

  return (
    <div className="space-y-5">
      <section aria-label="Filter approvals" className="flex flex-col gap-4 lg:flex-row lg:items-end lg:justify-between">
        <div role="group" aria-label="Approval type" className="flex flex-wrap gap-2">
          {KIND_FILTERS.map((filter) => (
            <button key={filter.label} type="button" aria-pressed={kind === filter.value} onClick={() => setKind(filter.value)} className={`min-h-11 rounded-full border px-4 text-sm font-medium transition-colors ${kind === filter.value ? "border-violet bg-violet text-white" : "border-edge bg-surface text-ink-soft hover:border-violet/40 hover:text-violet-deep"}`}>
              {filter.label}
            </button>
          ))}
        </div>
        <label className="text-sm font-medium text-ink">
          Status
          <select value={status} onChange={(event) => setStatus(event.target.value as ApprovalStatus)} className="mt-1.5 h-11 w-full rounded-chip border border-edge bg-surface px-3 text-sm font-normal text-ink lg:w-56">
            {STATUS_FILTERS.map((filter) => <option key={filter.value} value={filter.value}>{filter.label}</option>)}
          </select>
        </label>
      </section>

      {loading && <ReviewLoading label="Loading approvals" />}
      {!loading && error && <ReviewErrorState error={error} onRetry={() => setAttempt((value) => value + 1)} />}
      {!loading && !error && rows.length === 0 && (
        <EmptyState
          title={status === "pending" ? "Nothing is waiting on you" : "No approvals match"}
          body={status === "pending" ? "When a resume, cover letter or answers package needs your decision, it appears here." : "Try another status or type."}
        />
      )}
      {!loading && !error && rows.length > 0 && (
        <ul className="space-y-3" aria-label="Approvals">
          {rows.map((item) => {
            const app = applicationStatus(item.application_status);
            return (
              <li key={item.approval_id}>
                <Link href={approvalHref(item)} className="block rounded-card border border-edge bg-surface p-4 shadow-raised transition-colors hover:border-violet/40 focus-visible:border-violet sm:p-5">
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div className="min-w-0">
                      <p className="text-xs font-semibold uppercase tracking-[.12em] text-violet-deep">{APPROVAL_KIND_LABEL[item.kind]}</p>
                      <p className="mt-1 font-semibold text-ink">{item.job_title ?? "Untitled role"}</p>
                      <p className="text-sm text-ink-soft">{item.job_company ?? "Company unavailable"}</p>
                    </div>
                    <StatusBadge label={app.label} tone={app.tone} />
                  </div>
                  <p className="mt-3 text-xs text-ink-soft">
                    {item.status === "pending" ? `Waiting since ${formatTimestamp(item.requested_at)}` : `Decided ${item.decided_at ? formatTimestamp(item.decided_at) : "—"}`}
                    <span aria-hidden="true"> · </span>
                    <span className="font-semibold text-violet-deep">{item.status === "pending" ? "Review now" : "Open"} →</span>
                  </p>
                </Link>
              </li>
            );
          })}
        </ul>
      )}
      {!loading && !error && hasMore && (
        <button type="button" onClick={loadMore} disabled={loadingMore} className="min-h-11 w-full rounded-chip border border-edge bg-surface text-sm font-semibold text-violet-deep hover:border-violet/40 disabled:opacity-50">
          {loadingMore ? "Loading…" : "Load more approvals"}
        </button>
      )}
    </div>
  );
}
