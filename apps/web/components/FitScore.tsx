/**
 * Fit score.
 *
 * The most important number in the product, so it gets a treatment of its
 * own: a tabular numeral with a thin gauge beneath it. Not a donut - a donut
 * makes a single value harder to compare down a column, which is exactly
 * what this number is for.
 *
 * The band label is always rendered, so the score never depends on colour
 * alone to say whether it is good.
 */

export type FitBand = "strong" | "moderate" | "weak" | "no";

export function bandFor(score: number): FitBand {
  if (score >= 75) return "strong";
  if (score >= 60) return "moderate";
  if (score >= 40) return "weak";
  return "no";
}

const BAND_LABEL: Record<FitBand, string> = {
  strong: "Strong",
  moderate: "Moderate",
  weak: "Weak",
  no: "Low",
};

const BAND_BAR: Record<FitBand, string> = {
  strong: "bg-accent",
  moderate: "bg-action",
  weak: "bg-action",
  no: "bg-ink-soft",
};

const BAND_TEXT: Record<FitBand, string> = {
  strong: "text-success",
  moderate: "text-attention",
  weak: "text-attention",
  no: "text-ink-soft",
};

export function FitScore({
  score,
  showLabel = true,
  className = "",
}: {
  score: number;
  showLabel?: boolean;
  className?: string;
}) {
  const clamped = Math.max(0, Math.min(100, score));
  const band = bandFor(clamped);

  return (
    <div className={`w-[4.5rem] ${className}`}>
      <div className="flex items-baseline gap-1.5">
        <span className={`tabular text-lg font-semibold leading-none ${BAND_TEXT[band]}`}>
          {clamped.toFixed(0)}
        </span>
        {showLabel && (
          <span className="text-xs font-medium text-ink-soft">{BAND_LABEL[band]}</span>
        )}
      </div>
      <div
        className="mt-2 h-1.5 w-full overflow-hidden rounded-full bg-inset"
        role="meter"
        aria-valuenow={Math.round(clamped)}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-label={`Fit score ${clamped.toFixed(0)} of 100, ${BAND_LABEL[band]}`}
      >
        <div
          className={`h-full rounded-full ${BAND_BAR[band]}`}
          style={{ width: `${clamped}%` }}
        />
      </div>
    </div>
  );
}
