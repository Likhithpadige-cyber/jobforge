/**
 * Provenance tag: was this produced by fixed rules, or by a model?
 *
 * This is the one place the interface shouts, because it is the thing a
 * candidate most needs to know before trusting an output. A deterministic
 * score can be recomputed and audited; an AI-assisted one needs reading.
 * Uppercase is used here deliberately and nowhere else.
 */

export type Provenance = "deterministic" | "ai-assisted" | "human";

const STYLES: Record<Provenance, { label: string; className: string; title: string }> = {
  deterministic: {
    label: "Deterministic",
    className: "border-edge bg-inset text-ink-soft",
    title: "Produced by fixed rules. Same input always gives the same result.",
  },
  "ai-assisted": {
    label: "AI-assisted",
    className: "border-edge bg-inset text-attention",
    title: "A model contributed to this. Review before relying on it.",
  },
  human: {
    label: "You",
    className: "border-info bg-inset text-info",
    title: "You did this.",
  },
};

export function ProvenanceTag({ kind }: { kind: Provenance }) {
  const style = STYLES[kind];
  return (
    <span
      title={style.title}
      className={`inline-flex items-center rounded-chip border px-1.5 py-px text-micro font-semibold uppercase tracking-wide ${style.className}`}
    >
      {style.label}
    </span>
  );
}
