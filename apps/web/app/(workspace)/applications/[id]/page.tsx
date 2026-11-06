"use client";

import { use } from "react";
import { AppShell } from "@/components/AppShell";
import { ApplicationWorkspace } from "@/components/review/ApplicationWorkspace";

export default function Page({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  return (
    <AppShell title="Application" description="Materials, reviews and submission for this opportunity.">
      <div id="content" className="mx-auto max-w-[78rem]">
        <ApplicationWorkspace applicationId={id} />
      </div>
    </AppShell>
  );
}
