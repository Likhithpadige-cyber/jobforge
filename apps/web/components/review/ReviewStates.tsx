"use client";

import Link from "next/link";
import { EmptyState } from "@/components/EmptyState";
import { SkeletonRow } from "@/components/Skeleton";
import type { ReviewError } from "@/lib/review";

export function ReviewLoading({ label }: { label: string }) {
  return (
    <div role="status" aria-label={label} data-testid="review-loading" className="rounded-card border border-edge bg-surface">
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
          action={<Link href={backHref} className="inline-flex min-h-11 items-center rounded-chip border border-edge bg-surface px-4 text-sm font-semibold text-violet-deep hover:border-violet/40">Go back</Link>}
        />
      </div>
    );
  }
  return (
    <div role="alert" data-testid="review-error">
      <EmptyState
        title="Could not load this"
        body={error.message}
        action={onRetry ? <button type="button" onClick={onRetry} className="inline-flex min-h-11 items-center rounded-chip border border-edge bg-surface px-4 text-sm font-semibold text-violet-deep hover:border-violet/40">Try again</button> : undefined}
      />
    </div>
  );
}

export function SectionCard({ title, children, action }: { title: string; children: React.ReactNode; action?: React.ReactNode }) {
  return (
    <section aria-label={title} className="rounded-[1.25rem] border border-edge bg-surface p-5 shadow-raised sm:p-6">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="text-base font-semibold text-ink">{title}</h3>
        {action}
      </div>
      <div className="mt-3">{children}</div>
    </section>
  );
}
