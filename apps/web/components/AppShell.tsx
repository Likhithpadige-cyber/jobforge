"use client";

import type { ReactNode } from "react";
import { Header } from "./Header";
import { SignOutButton } from "./SignOutButton";
import { useWorkspaceShell } from "./WorkspaceShell";
import { GenerationPauseBanner } from "./operations/GenerationPauseBanner";

/**
 * One page inside the persistent workspace frame: its header, the
 * operations banner and its content. The sidebar, navigation, session
 * guard and operations state live in `WorkspaceShell`, which the
 * `app/(workspace)` layout mounts once, so changing page never remounts them.
 */
export function AppShell({
  title,
  description,
  children,
}: {
  title: string;
  description?: string;
  children: ReactNode;
}) {
  const { openNav } = useWorkspaceShell();
  return (
    <>
      <Header title={title} description={description} onOpenNav={openNav} />
      <GenerationPauseBanner />
      <main className="animate-page-in px-5 py-7 pb-28 lg:px-9 lg:py-9">{children}</main>
      <div className="px-5 pb-28 lg:px-9 lg:pb-6">
        <SignOutButton />
      </div>
    </>
  );
}
