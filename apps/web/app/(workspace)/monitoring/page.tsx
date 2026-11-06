"use client";

import { AppShell } from "@/components/AppShell";
import { MonitoringView } from "@/components/operations/MonitoringView";
import { OwnerOnly } from "@/components/operations/OperatorStates";

export default function Page() {
  return (
    <AppShell title="Monitoring" description="Worker health, deliveries, submissions and backlog from the monitor's snapshots.">
      <div id="content" className="mx-auto max-w-[84rem]">
        <OwnerOnly>
          <MonitoringView />
        </OwnerOnly>
      </div>
    </AppShell>
  );
}
