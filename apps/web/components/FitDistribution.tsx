type Band = { band: string; range: string; count: number; tone: "strong" | "moderate" | "low" };

const BAR: Record<Band["tone"], string> = {
  strong: "bg-signal-strong",
  moderate: "bg-brass",
  low: "bg-ink-soft/40",
};

/**
 * How the evaluated jobs are distributed across fit bands.
 *
 * A stacked horizontal bar rather than a pie: the useful question is "what
 * share of what JobForge found is actually worth my time", and proportion of
 * a line answers that at a glance. Counts are printed as well as drawn.
 */
export function FitDistribution({ bands }: { bands: Band[] }) {
  const total = bands.reduce((sum, b) => sum + b.count, 0) || 1;

  return (
    <div>
      <div
        className="flex h-2.5 w-full overflow-hidden rounded-full bg-muted"
        role="img"
        aria-label={bands
          .map((b) => `${b.band} ${b.count} of ${total}`)
          .join(", ")}
      >
        {bands.map((b) => (
          <div
            key={b.band}
            className={BAR[b.tone]}
            style={{ width: `${(b.count / total) * 100}%` }}
          />
        ))}
      </div>

      <dl className="mt-4 space-y-2.5">
        {bands.map((b) => (
          <div key={b.band} className="flex items-baseline gap-3">
            <span
              className={`h-2 w-2 shrink-0 rounded-full ${BAR[b.tone]}`}
              aria-hidden="true"
            />
            <dt className="text-sm text-ink">{b.band}</dt>
            <span className="text-xs text-ink-soft">{b.range}</span>
            <dd className="tabular ml-auto text-sm font-medium text-ink">
              {b.count}
              <span className="ml-1.5 text-xs font-normal text-ink-soft">
                {Math.round((b.count / total) * 100)}%
              </span>
            </dd>
          </div>
        ))}
      </dl>
    </div>
  );
}
