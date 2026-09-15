/**
 * Claim verdict badge.
 *
 * Thin wrapper over StatusBadge so every verdict in the studio looks the same
 * and the tone mapping lives in one place. Colour is never the only signal:
 * the label is always present as text.
 */

import { StatusBadge } from "@/components/StatusBadge";
import { VERDICT_LABEL, VERDICT_TONE } from "@/lib/resumes";
import type { ClaimVerdict } from "@/lib/api";

export function VerdictBadge({
  verdict,
  className = "",
}: {
  verdict: ClaimVerdict;
  className?: string;
}) {
  return (
    <StatusBadge
      label={VERDICT_LABEL[verdict]}
      tone={VERDICT_TONE[verdict]}
      className={className}
    />
  );
}
