"use client";

import { useState, type ReactNode } from "react";
import { AuthGuard } from "./AuthGuard";
import { Header } from "./Header";
import { Sidebar } from "./Sidebar";
import { SignOutButton } from "./SignOutButton";
import { MobileNav } from "./MobileNav";
import { GenerationPauseBanner } from "./operations/GenerationPauseBanner";
import { OperationsProvider } from "./operations/OperationsProvider";

/**
 * Desktop-first two-column shell. Below `lg` the sidebar becomes a drawer
 * rather than disappearing, so navigation is never lost on a narrow screen.
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
  const [navOpen, setNavOpen] = useState(false);

  // Every page rendered through the shell is a protected page, so the guard
  // lives here rather than being repeated per route. /login does not use the
  // shell and is therefore reachable signed out.
  return (
    <AuthGuard>
    <OperationsProvider>
    <div className="min-h-screen lg:grid lg:grid-cols-[15rem_1fr]">
      <aside className="hidden lg:block">
        <div className="sticky top-0 h-screen">
          <Sidebar />
        </div>
      </aside>

      {navOpen && (
        <div className="fixed inset-0 z-40 lg:hidden">
          <button
            type="button"
            className="absolute inset-0 bg-ink/40"
            aria-label="Close navigation"
            onClick={() => setNavOpen(false)}
          />
          <div className="relative h-full w-[17rem] animate-panel-in">
            <Sidebar onNavigate={() => setNavOpen(false)} />
          </div>
        </div>
      )}

      <div className="min-w-0">
        <Header
          title={title}
          description={description}
          onOpenNav={() => setNavOpen(true)}
        />
        <GenerationPauseBanner />
        <main className="animate-page-in px-5 py-7 pb-28 lg:px-9 lg:py-9">{children}</main>
        <div className="px-5 pb-28 lg:px-9 lg:pb-6">
          <SignOutButton />
        </div>
      </div>
      <MobileNav onOpenMore={() => setNavOpen(true)} />
    </div>
    </OperationsProvider>
    </AuthGuard>
  );
}
