/**
 * Status badge.
 *
 * Colour is never the only signal: every badge carries its label as text, so
 * the meaning survives greyscale printing and colour-blindness.
 */

export type StatusTone = "neutral" | "strong" | "review" | "risk" | "info" | "brass";

const TONES: Record<StatusTone, string> = {
  neutral: "border-edge bg-inset text-ink-soft",
  strong: "border-success bg-inset text-success",
  review: "border-attention bg-inset text-attention",
  risk: "border-danger bg-inset text-danger",
  info: "border-info bg-inset text-info",
  brass: "border-edge bg-inset text-attention",
};

/** Maps a job/application status onto a tone. */
export const STATUS_TONES: Record<string, StatusTone> = {
  discovered: "neutral",
  normalized: "neutral",
  evaluated: "review",
  shortlisted: "strong",
  queued: "info",
  "resume ready": "brass",
  "awaiting approval": "review",
  approved: "strong",
  submitted: "info",
  applied: "info",
  interview: "strong",
  discarded: "neutral",
  blocked: "risk",
};

export function StatusBadge({
  label,
  tone,
  className = "",
}: {
  label: string;
  tone?: StatusTone;
  className?: string;
}) {
  const resolved = tone ?? STATUS_TONES[label.toLowerCase()] ?? "neutral";
  return (
    <span
      className={`inline-flex items-center rounded-chip border px-2 py-0.5 text-xs font-medium ${TONES[resolved]} ${className}`}
    >
      {label}
    </span>
  );
}
