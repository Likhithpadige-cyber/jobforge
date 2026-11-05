"use client";

import Link from "next/link";
import { Icon } from "./Icon";
import { StatusBadge } from "./StatusBadge";
import { Skeleton } from "./Skeleton";
import { EmptyState } from "./EmptyState";
import type { ApiJob } from "@/lib/api";
import {
  STATUS_LABEL,
  WORK_MODE_LABEL,
  formatLocation,
  formatSalary,
  needsHumanDecision,
  relativeTime,
  workMode,
} from "@/lib/jobs";

/**
 * The feed list.
 *
 * A table rather than cards: the whole job of this screen is letting you run
 * your eye down a column and pick the next thing to look at. Cards make that
 * slower and take four times the vertical space.
 */
export function JobFeedTable({
  jobs,
  sourceNames,
  loading,
  hasFilters,
  onResetFilters,
}: {
  jobs: ApiJob[];
  sourceNames: Record<string, string>;
  loading: boolean;
  hasFilters: boolean;
  onResetFilters: () => void;
}) {
  if (loading) {
    return (
      <div className="overflow-hidden rounded-card border border-edge bg-surface">
        {Array.from({ length: 6 }).map((_, index) => (
          <div
            key={index}
            className="flex items-center gap-4 border-b border-edge px-5 py-4 last:border-0"
          >
            <Skeleton className="h-4 w-56" />
            <Skeleton className="h-4 w-28" />
            <Skeleton className="ml-auto h-7 w-28" />
          </div>
        ))}
      </div>
    );
  }

  if (jobs.length === 0) {
    return hasFilters ? (
      <EmptyState
        title="No jobs match these filters"
        body="Try a broader search, or clear the filters to see everything that has been found."
        action={
          <button
            type="button"
            onClick={onResetFilters}
            className="rounded-chip border border-edge bg-surface px-3 py-1.5 text-sm font-medium text-ink hover:border-control hover:text-attention"
          >
            Clear filters
          </button>
        }
      />
    ) : (
      <EmptyState
        title="No jobs yet"
        body="Import postings from a CSV or JSON export and JobForge will read the requirements out of each one."
      />
    );
  }

  return (
    <div className="overflow-hidden rounded-card border border-edge bg-surface shadow-raised">
      <div className="overflow-x-auto">
        <table className="w-full min-w-[58rem] text-left text-sm">
          <caption className="sr-only">
            Jobs found, with work mode, status, source and when each was found
          </caption>
          <thead>
            <tr className="border-b border-edge bg-inset text-xs text-ink-soft">
              <th scope="col" className="px-5 py-3 font-medium">Role</th>
              <th scope="col" className="px-5 py-3 font-medium">Location</th>
              <th scope="col" className="hidden px-5 py-3 font-medium lg:table-cell">
                What it asks for
              </th>
              <th scope="col" className="px-5 py-3 font-medium">Status</th>
              <th scope="col" className="hidden px-5 py-3 font-medium xl:table-cell">Source</th>
              <th scope="col" className="hidden px-5 py-3 font-medium md:table-cell">Found</th>
              <th scope="col" className="px-5 py-3 text-right font-medium">Next step</th>
            </tr>
          </thead>
          <tbody>
            {jobs.map((job) => {
              const mode = workMode(job);
              const salary = formatSalary(job);
              const decide = needsHumanDecision(job);
              return (
                <tr key={job.id} className="border-b border-edge last:border-0 hover:bg-inset">
                  <td className="px-5 py-4 align-top">
                    <p className="font-medium leading-snug text-ink">{job.title}</p>
                    <p className="mt-1 flex flex-wrap items-center gap-x-2 text-xs text-ink-soft">
                      <span>{WORK_MODE_LABEL[mode]}</span>
                      {job.seniority && job.seniority !== "unknown" && (
                        <>
                          <span aria-hidden="true">·</span>
                          <span className="capitalize">{job.seniority}</span>
                        </>
                      )}
                      {salary && (
                        <>
                          <span aria-hidden="true">·</span>
                          <span className="tabular">{salary}</span>
                        </>
                      )}
                    </p>
                  </td>
                  <td className="px-5 py-4 align-top text-sm text-ink-soft">
                    {formatLocation(job)}
                  </td>
                  <td className="hidden px-5 py-4 align-top text-sm text-ink-soft lg:table-cell">
                    Open the job to see its requirements
                  </td>
                  <td className="px-5 py-4 align-top">
                    <StatusBadge
                      label={STATUS_LABEL[job.status] ?? job.status}
                      tone={decide ? "review" : undefined}
                    />
                    {decide && (
                      <p className="mt-1 text-xs text-attention">Waiting on you</p>
                    )}
                  </td>
                  <td className="hidden px-5 py-4 align-top text-sm text-ink-soft xl:table-cell">
                    {sourceNames[job.source_id] ?? "—"}
                  </td>
                  <td className="hidden px-5 py-4 align-top text-sm text-ink-soft md:table-cell">
                    {relativeTime(job.first_seen_at)}
                  </td>
                  <td className="px-5 py-4 text-right align-top">
                    <Link
                      href={`/job-feed/${job.id}`}
                      className="inline-flex items-center gap-1.5 whitespace-nowrap rounded-chip border border-edge bg-surface px-3 py-1.5 text-sm font-medium text-ink transition-transform hover:border-control hover:bg-inset hover:text-attention"
                    >
                      Review match
                      <Icon name="arrow" className="h-3.5 w-3.5" />
                    </Link>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
