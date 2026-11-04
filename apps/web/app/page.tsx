"use client";

import { AppShell } from "@/components/AppShell";
import { Dashboard } from "@/components/dashboard/Dashboard";

export default function OverviewPage() {
  return (
    <AppShell title="Dashboard" description="A clear view of what matters next in your career search.">
      <div id="content" className="mx-auto max-w-[84rem]">
        <Dashboard />
      </div>
    </AppShell>
  );
}
