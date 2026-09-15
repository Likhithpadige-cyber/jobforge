import { Icon } from "./Icon";
import type { DemoAttention } from "@/lib/demo-data";

/**
 * An item only a person can settle.
 *
 * Each kind is distinguishable three ways at once - icon, tinted icon well,
 * and a named kind label - so the difference between "waiting on you" and
 * "something was flagged" survives greyscale and colour-blindness. The rule
 * on the left is the only saturated element; the card itself stays white,
 * which keeps the section useful rather than alarming.
 */
const KIND = {
  review: {
    icon: "approvals",
    label: "Waiting on you",
    text: "text-signal-review",
    well: "bg-signal-review/10",
    rule: "bg-signal-review",
  },
  risk: {
    icon: "shield",
    label: "Flagged",
    text: "text-signal-risk",
    well: "bg-signal-risk/10",
    rule: "bg-signal-risk",
  },
  info: {
    icon: "profile",
    label: "Worth doing",
    text: "text-signal-info",
    well: "bg-signal-info/10",
    rule: "bg-signal-info",
  },
} as const;

export function ReviewAlert({ item }: { item: DemoAttention }) {
  const style = KIND[item.kind];
  return (
    <div className="relative overflow-hidden rounded-card border border-edge bg-surface shadow-raised">
      <span
        className={`absolute left-0 top-0 h-full w-1 ${style.rule}`}
        aria-hidden="true"
      />
      <div className="flex items-start gap-3.5 p-4 pl-5">
        <span
          className={`mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-chip ${style.well}`}
          aria-hidden="true"
        >
          <Icon name={style.icon} className={`h-4 w-4 ${style.text}`} />
        </span>
        <div className="min-w-0">
          <p className={`text-xs font-medium ${style.text}`}>{style.label}</p>
          <p className="mt-0.5 text-sm font-medium leading-snug text-ink">
            {item.title}
          </p>
          <p className="mt-1.5 text-sm leading-relaxed text-ink-soft">{item.body}</p>
          <button
            type="button"
            className="mt-3 inline-flex items-center gap-1 text-sm font-medium text-brass-deep hover:underline"
          >
            {item.cta}
            <Icon name="arrow" className="h-3.5 w-3.5" />
          </button>
        </div>
      </div>
    </div>
  );
}
