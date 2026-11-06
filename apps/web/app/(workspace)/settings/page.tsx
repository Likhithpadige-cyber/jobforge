"use client";

import { AppShell } from "@/components/AppShell";
import { SectionCard } from "@/components/review/ReviewStates";
import { SubmissionSwitchCard } from "@/components/settings/SubmissionSwitchCard";

export default function Page() {
  return (
    <AppShell title="Settings" description="Safety switches and how this workspace is configured.">
      <div id="content" className="mx-auto max-w-[56rem] space-y-6">
        <SubmissionSwitchCard />
        <SectionCard title="Matching and limits">
          <p className="text-sm text-ink-soft">
            Fit thresholds and the daily application limit are set in this deployment&apos;s configuration. They are not editable here yet, so this page does not show values it cannot confirm.
          </p>
        </SectionCard>
      </div>
    </AppShell>
  );
}
