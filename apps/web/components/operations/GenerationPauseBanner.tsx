"use client";

import Link from "next/link";
import { Icon } from "@/components/Icon";
import { useOperations } from "./OperationsProvider";

/**
 * Shown on every page while the server reports a generation pause.
 *
 * It renders the last confirmed server value only. When a refresh fails the
 * banner stays and says its status may be out of date; it never claims that
 * generation resumed.
 */
export function GenerationPauseBanner() {
  const { enabled, pause, pauseStale } = useOperations();
  if (!enabled || !pause?.generation_paused) {
    if (enabled && pause === null && pauseStale) {
      return (
        <div role="status" aria-live="polite" className="border-b border-signal-review/25 bg-signal-review/10 px-5 py-2.5 text-sm text-ink lg:px-9">
          Generation status could not be confirmed. Operations data may be unavailable.
        </div>
      );
    }
    return null;
  }
  const count = pause.unresolved_auto_pause_count;
  return (
    <div
      role="status"
      aria-live="polite"
      data-testid="generation-pause-banner"
      className="border-b border-signal-risk/25 bg-signal-risk/10 px-5 py-3 lg:px-9"
    >
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
        <Icon name="shield" className="h-5 w-5 shrink-0 text-signal-risk" />
        <p className="min-w-0 flex-1 text-sm text-ink">
          <strong className="font-semibold text-signal-risk">Generation is paused.</strong>{" "}
          {count === 1
            ? "1 safety incident needs your review before resumes or cover letters can be generated."
            : `${count} safety incidents need your review before resumes or cover letters can be generated.`}
          {pauseStale && (
            <span className="ml-1 font-medium text-signal-review">
              Status may be out of date: the last refresh failed.
            </span>
          )}
        </p>
        <Link
          href="/alerts?pausing=1"
          className="inline-flex min-h-10 items-center rounded-chip border border-signal-risk/30 bg-surface px-3 text-sm font-semibold text-signal-risk hover:bg-signal-risk/5"
        >
          Review incidents <Icon name="arrow" className="ml-1.5 h-3.5 w-3.5" />
        </Link>
      </div>
    </div>
  );
}
