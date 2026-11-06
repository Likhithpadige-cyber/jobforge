"use client";

import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { AuthGuard } from "./AuthGuard";
import { MobileNav } from "./MobileNav";
import { Sidebar } from "./Sidebar";
import { OperationsProvider } from "./operations/OperationsProvider";

/**
 * The persistent workspace frame, mounted once by `app/(workspace)/layout.tsx`.
 *
 * Everything that must not flash between pages lives here: the session
 * guard, the operations state behind the alert bell and pause banner, the
 * sidebar, the mobile drawer and the bottom navigation. A route change
 * swaps only the page (`AppShell`: header and content) inside it, so the
 * sidebar keeps its DOM, its selection animates instead of re-appearing,
 * and the session is not re-checked behind a blank screen.
 */
const ShellContext = createContext<{ openNav: () => void }>({ openNav: () => {} });

export function useWorkspaceShell() {
  return useContext(ShellContext);
}

export function WorkspaceShell({ children }: { children: ReactNode }) {
  const [navOpen, setNavOpen] = useState(false);
  const drawer = useRef<HTMLDivElement>(null);
  const openNav = useCallback(() => setNavOpen(true), []);

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

  return (
    <AuthGuard>
      <OperationsProvider>
        <ShellContext.Provider value={{ openNav }}>
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

            <div className="min-w-0">{children}</div>
            <MobileNav onOpenMore={openNav} />
          </div>
        </ShellContext.Provider>
      </OperationsProvider>
    </AuthGuard>
  );
}
