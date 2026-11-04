"use client";

import { AppShell } from "@/components/AppShell";
import { AgentActivityView } from "@/components/activity/AgentActivityView";

export default function Page() {
  return (
    <AppShell title="Agent Activity" description="What ran, what it cost and how it ended.">
      <div id="content" className="mx-auto max-w-[72rem]">
        <AgentActivityView />
      </div>
    </AppShell>
  );
}
