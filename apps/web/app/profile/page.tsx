"use client";

import { AppShell } from "@/components/AppShell";
import { ProfileView } from "@/components/profile/ProfileView";

export default function Page() {
  return (
    <AppShell title="Profile & Facts" description="The verified facts everything written about you must trace back to.">
      <div id="content" className="mx-auto max-w-[72rem]">
        <ProfileView />
      </div>
    </AppShell>
  );
}
