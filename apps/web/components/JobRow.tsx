import { FitScore } from "./FitScore";
import { StatusBadge } from "./StatusBadge";
import { Icon } from "./Icon";
import type { DemoJob } from "@/lib/demo-data";

/**
 * One row of the recommended-jobs table.
 *
 * Every row answers the same five questions in the same order: what the job
 * is, where, how well it fits, why that number, and what to do next. The
 * match explanation sits directly under the role rather than beside the
 * score, because it reads as a sentence about the job, not about the number.
 *
 * Deliberately a table row and not a card: a column of scores is only
 * comparable when the numbers line up.
 */
export function JobRow({ job }: { job: DemoJob }) {
  return (
    <tr className="group border-b border-edge last:border-0 hover:bg-muted/50">
      <td className="px-5 py-4 align-top">
        <p className="font-medium leading-snug text-ink">{job.role}</p>
        <p className="mt-1 text-sm text-ink-soft">
          {job.company}
          <span className="px-1.5 text-edge" aria-hidden="true">
            |
          </span>
          {job.location}
        </p>
      </td>
      <td className="px-5 py-4 align-top">
        <FitScore score={job.fit} />
      </td>
      <td className="hidden px-5 py-4 align-top text-sm text-ink-soft md:table-cell">
        <p>
          Covers {job.matched} of {job.matched + job.missing} requirements
        </p>
        {job.topGap && (
          <p className="mt-1 text-xs">Missing: {job.topGap}</p>
        )}
      </td>
      <td className="px-5 py-4 align-top">
        <StatusBadge label={job.status} />
      </td>
      <td className="hidden px-5 py-4 align-top text-sm text-ink-soft lg:table-cell">
        {job.foundAgo}
      </td>
      <td className="px-5 py-4 text-right align-top">
        <button
          type="button"
          className="inline-flex items-center gap-1.5 whitespace-nowrap rounded-chip border border-edge bg-surface px-3 py-1.5 text-sm font-medium text-ink transition-colors hover:border-brass hover:bg-brass-soft/50 hover:text-brass-deep"
        >
          Review match
          <Icon name="arrow" className="h-3.5 w-3.5" />
        </button>
      </td>
    </tr>
  );
}
