import type { ApiRequirement } from "@/lib/api";
import { REQUIREMENT_KIND_LABEL, formatYears } from "@/lib/jobs";

/**
 * What the employer asked for.
 *
 * Match state is deliberately absent: nothing in the backend knows yet
 * whether a requirement is met, and a grey "unknown" dot against every line
 * would read as "we checked and found nothing". The panel beside this says
 * plainly that matching is not wired up.
 */
export function RequirementList({
  title,
  requirements,
  emptyNote,
}: {
  title: string;
  requirements: ApiRequirement[];
  emptyNote: string;
}) {
  return (
    <section>
      <div className="flex items-baseline justify-between pb-2">
        <h3 className="text-sm font-medium text-ink">{title}</h3>
        <span className="tabular text-xs text-ink-soft">{requirements.length}</span>
      </div>

      {requirements.length === 0 ? (
        <p className="rounded-card border border-dashed border-edge px-4 py-3 text-sm text-ink-soft">
          {emptyNote}
        </p>
      ) : (
        <ul className="divide-y divide-edge overflow-hidden rounded-card border border-edge bg-surface">
          {requirements.map((req, index) => {
            const years = formatYears(req);
            return (
              <li key={`${req.text}-${index}`} className="px-4 py-3">
                <p className="text-sm leading-relaxed text-ink">{req.text}</p>
                <div className="mt-1.5 flex flex-wrap items-center gap-2 text-xs text-ink-soft">
                  <span className="rounded-chip border border-edge bg-muted px-1.5 py-px">
                    {REQUIREMENT_KIND_LABEL[req.kind] ?? req.kind}
                  </span>
                  {years && <span className="tabular">{years}</span>}
                  {req.section && <span>from “{req.section}”</span>}
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
