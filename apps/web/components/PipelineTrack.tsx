import type { PipelineStage } from "@/lib/dashboard-priority";

/**
 * Where everything stands.
 *
 * An ordered sequence, because the workflow genuinely is one. The stage that
 * needs a person is marked three ways - brass numeral, brass bar, and a
 * "waiting on you" line - rather than by flooding the cell with colour. Every
 * other stage stays neutral so the one that matters is the one that stands out.
 */
export function PipelineTrack({ stages }: { stages: PipelineStage[] }) {
  const peak = Math.max(...stages.map((s) => s.count), 1);

  return (
    <ol className="grid grid-cols-2 gap-px overflow-hidden rounded-card border border-edge bg-edge sm:grid-cols-3 lg:grid-cols-6">
      {stages.map((stage, index) => {
        const needsYou = Boolean(stage.note);
        return (
          <li
            key={stage.key}
            className={`relative bg-surface p-4 ${needsYou ? "bg-brass-soft/25" : ""}`}
          >
            {needsYou && (
              <span
                className="absolute inset-x-0 top-0 h-0.5 bg-brass"
                aria-hidden="true"
              />
            )}
            <div className="flex items-center gap-1.5">
              <span
                className={`tabular text-xs font-medium ${
                  needsYou ? "text-brass-deep" : "text-ink-soft/70"
                }`}
              >
                {index + 1}
              </span>
              <p className="truncate text-xs text-ink-soft">{stage.label}</p>
            </div>
            <p
              className={`tabular mt-2 text-xl font-semibold leading-none ${
                needsYou ? "text-brass-deep" : "text-ink"
              }`}
            >
              {stage.count}
            </p>
            <div className="mt-3 h-1 w-full rounded-full bg-muted">
              <div
                className={`h-full rounded-full ${needsYou ? "bg-brass" : "bg-ink/20"}`}
                style={{ width: `${Math.max(6, (stage.count / peak) * 100)}%` }}
              />
            </div>
            <p
              className={`mt-2 text-xs ${
                needsYou ? "font-medium text-brass-deep" : "text-transparent"
              }`}
              aria-hidden={!needsYou}
            >
              {stage.note ?? "\u00a0"}
            </p>
          </li>
        );
      })}
    </ol>
  );
}
