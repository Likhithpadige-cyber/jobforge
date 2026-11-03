"use client";

import { use } from "react";
import { AppShell } from "@/components/AppShell";
import { AlertDetailView } from "@/components/operations/AlertDetailView";
import { OwnerOnly } from "@/components/operations/OperatorStates";

export default function Page({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  return (
    <AppShell title="Alert" description="One incident, its policy and its resolution.">
      <div id="content" className="mx-auto max-w-[56rem]">
        <OwnerOnly>
          <AlertDetailView id={id} />
        </OwnerOnly>
      </div>
    </AppShell>
  );
}
