"use client";

import { useEffect, useRef, useState, type ReactNode } from "react";
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
  const drawer = useRef<HTMLDivElement>(null);

  // The drawer is a modal dialog: focus moves into it, Tab stays inside,
  // Escape closes it, and focus returns to whatever opened it.
  useEffect(() => {
    if (!navOpen) return;
    const opener = document.activeElement as HTMLElement | null;
    drawer.current?.querySelector<HTMLElement>("a[href], button")?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setNavOpen(false);
        return;
      }
      if (event.key !== "Tab" || !drawer.current) return;
      const focusable = Array.from(drawer.current.querySelectorAll<HTMLElement>("a[href], button:not([disabled])"));
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (!first || !last) return;
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      opener?.focus();
    };
  }, [navOpen]);

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
            tabIndex={-1}
            className="absolute inset-0 bg-canvas"
            aria-label="Close navigation"
            onClick={() => setNavOpen(false)}
          />
          <div ref={drawer} role="dialog" aria-modal="true" aria-label="Navigation" data-testid="nav-drawer" className="relative h-full w-[17rem] max-w-[85vw] animate-panel-in">
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
