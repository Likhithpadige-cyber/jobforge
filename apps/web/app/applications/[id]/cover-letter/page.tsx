"use client";

import { use } from "react";
import { AppShell } from "@/components/AppShell";
import { CoverLetterReviewView } from "@/components/review/CoverLetterReviewView";

export default function Page({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  return (
    <AppShell title="Cover letter review" description="Read every paragraph against its evidence before deciding.">
      <div id="content" className="mx-auto max-w-[60rem]">
        <CoverLetterReviewView applicationId={id} />
      </div>
    </AppShell>
  );
}
