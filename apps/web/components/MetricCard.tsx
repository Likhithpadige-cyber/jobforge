import type { ReactNode } from "react";
import { Icon, type IconName } from "./Icon";

/**
 * A headline number.
 *
 * The delta line is optional and stays absent when there is nothing true to
 * say - an invented "+12% vs last week" on a product with four days of data
 * would be worse than a blank space.
 */
export function MetricCard({
  label,
  value,
  hint,
  icon,
  tone = "neutral",
  children,
}: {
  label: string;
  value: string | number;
  hint?: string;
  icon?: IconName;
  tone?: "neutral" | "brass" | "review";
  children?: ReactNode;
}) {
  const accent =
    tone === "brass"
      ? "text-attention"
      : tone === "review"
        ? "text-attention"
        : "text-ink-soft";

  return (
    <div className="rounded-card border border-edge bg-surface p-5 shadow-raised">
      <div className="flex items-start justify-between">
        <p className="text-sm text-ink-soft">{label}</p>
        {icon && <Icon name={icon} className={`h-4 w-4 ${accent}`} />}
      </div>
      <p className="metric-value tabular mt-4 text-metric text-ink">{value}</p>
      {hint && <p className="mt-1.5 text-xs leading-relaxed text-ink-soft">{hint}</p>}
      {children}
    </div>
  );
}
