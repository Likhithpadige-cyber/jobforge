"use client";

import { AppShell } from "@/components/AppShell";
import { ApprovalsInbox } from "@/components/review/ApprovalsInbox";

export default function Page() {
  return (
    <AppShell title="Approvals" description="Nothing leaves JobForge without your sign-off.">
      <div id="content" className="mx-auto max-w-[72rem]">
        <ApprovalsInbox />
      </div>
    </AppShell>
  );
}
