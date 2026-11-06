"use client";

import { use } from "react";
import { AppShell } from "@/components/AppShell";
import { AlertsView } from "@/components/operations/AlertsView";
import { OwnerOnly } from "@/components/operations/OperatorStates";

export default function Page({ searchParams }: { searchParams: Promise<{ pausing?: string }> }) {
  const { pausing } = use(searchParams);
  return (
    <AppShell title="Alerts" description="Operational and safety incidents, and their resolution.">
      <div id="content" className="mx-auto max-w-[72rem]">
        <OwnerOnly>
          <AlertsView initialPausingOnly={pausing === "1"} />
        </OwnerOnly>
      </div>
    </AppShell>
  );
}
