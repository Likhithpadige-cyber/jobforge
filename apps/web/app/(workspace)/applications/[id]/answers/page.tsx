"use client";

import { use } from "react";
import { AppShell } from "@/components/AppShell";
import { AnswersPackageView } from "@/components/review/AnswersPackageView";

export default function Page({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  return (
    <AppShell title="Application answers" description="Check every answer before approving the package.">
      <div id="content" className="mx-auto max-w-[60rem]">
        <AnswersPackageView applicationId={id} />
      </div>
    </AppShell>
  );
}
