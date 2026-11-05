"use client";

import Link from "next/link";
import { useId } from "react";
import { EmptyState } from "@/components/EmptyState";
import { SkeletonRow } from "@/components/Skeleton";
import type { ReviewError } from "@/lib/review";

export function ReviewLoading({ label }: { label: string }) {
  return (
    <div role="status" aria-label={label} data-testid="review-loading" className="rounded-card border border-edge bg-surface">
      <span className="sr-only">{label}…</span>
      <SkeletonRow />
      <SkeletonRow />
      <SkeletonRow />
    </div>
  );
}

/** One place that turns an API failure into the right state for the reader. */
export function ReviewErrorState({ error, onRetry, backHref = "/approvals" }: { error: ReviewError; onRetry?: () => void; backHref?: string }) {
  if (error.kind === "forbidden") {
    return (
      <div data-testid="review-forbidden">
        <EmptyState title="You cannot open this" body={error.message} />
      </div>
    );
  }
  if (error.kind === "not_found") {
    return (
      <div role="alert" data-testid="review-not-found">
        <EmptyState
          title="Not found"
          body={error.message}
          action={<Link href={backHref} className="inline-flex min-h-11 items-center rounded-chip border border-edge bg-surface px-4 text-sm font-semibold text-accent hover:border-control">Go back</Link>}
        />
      </div>
    );
  }
  return (
    <div role="alert" data-testid="review-error">
      <EmptyState
        title="Could not load this"
        body={error.message}
        action={onRetry ? <button type="button" onClick={onRetry} className="inline-flex min-h-11 items-center rounded-chip border border-edge bg-surface px-4 text-sm font-semibold text-accent hover:border-control">Try again</button> : undefined}
      />
    </div>
  );
}

export function SectionCard({ title, children, action }: { title: string; children: React.ReactNode; action?: React.ReactNode }) {
  const headingId = useId();
  return (
    <section aria-labelledby={headingId} className="rounded-[1.25rem] border border-edge bg-surface p-5 shadow-raised sm:p-6">
      <div className="flex flex-wrap items-center justify-between gap-2">
        {/* h2: every page has an h1 in the header, so a card never skips a level. */}
        <h2 id={headingId} className="text-base font-semibold text-ink">{title}</h2>
        {action}
      </div>
      <div className="mt-3">{children}</div>
    </section>
  );
}
