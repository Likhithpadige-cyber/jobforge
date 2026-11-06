"use client";

import { use } from "react";
import { AppShell } from "@/components/AppShell";
import { SubmissionView } from "@/components/review/SubmissionView";

export default function Page({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  return (
    <AppShell title="Submission" description="Readiness, the safety switch, and the human-authorized submit.">
      <div id="content" className="mx-auto max-w-[60rem]">
        <SubmissionView applicationId={id} />
      </div>
    </AppShell>
  );
}
