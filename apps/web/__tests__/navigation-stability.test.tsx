/**
 * Regression tests for the navigation flash.
 *
 * Root cause: every page rendered its own AppShell, so a route change
 * unmounted and remounted the sidebar, header, session guard and operations
 * state; and AuthGuard re-checked the session on every pathname change by
 * replacing the whole UI with a loading line. Together that blanked the
 * screen (no sidebar, no header, no content) between pages.
 */
import { existsSync, readFileSync, readdirSync } from "node:fs";
import path from "node:path";
import { act, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, api } from "@/lib/api";

const nav = vi.hoisted(() => ({ pathname: "/", replace: vi.fn(), push: vi.fn() }));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: nav.replace, push: nav.push }),
  usePathname: () => nav.pathname,
}));

vi.mock("@/components/operations/OperationsProvider", () => ({
  OperationsProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  useOperations: () => ({ enabled: false, openAlerts: null, pause: null }),
}));

vi.mock("@/components/operations/GenerationPauseBanner", () => ({ GenerationPauseBanner: () => null }));

import { AppShell } from "@/components/AppShell";
import { WorkspaceShell } from "@/components/WorkspaceShell";

const OWNER = { id: "u1", email: "demo.candidate@example.com", role: "owner", last_login_at: null };

function Page({ title }: { title: string }) {
  return <AppShell title={title}><p>{title} content</p></AppShell>;
}

/** Renders the persistent layout around a page, the way app/(workspace)/layout.tsx does. */
function renderAt(pathname: string, title: string) {
  nav.pathname = pathname;
  return render(<WorkspaceShell><Page title={title} /></WorkspaceShell>);
}

beforeEach(() => {
  nav.pathname = "/";
  nav.replace.mockReset();
});

describe("persistent workspace shell", () => {
  it("keeps the same sidebar and never shows the session check while changing page", async () => {
    const me = vi.spyOn(api, "me").mockResolvedValue(OWNER);
    const view = renderAt("/", "Dashboard");
    const sidebar = await screen.findByRole("navigation", { name: "Main" });
    expect(me).toHaveBeenCalledTimes(1);

    for (const [href, title] of [["/applications", "Applications"], ["/job-feed", "Job Feed"], ["/settings", "Settings"]] as const) {
      nav.pathname = href;
      view.rerender(<WorkspaceShell><Page title={title} /></WorkspaceShell>);
      // Synchronously after the route change: the frame is still there.
      expect(screen.queryByTestId("auth-loading")).not.toBeInTheDocument();
      expect(screen.getByRole("navigation", { name: "Main" })).toBe(sidebar);
      expect(screen.getByRole("heading", { level: 1, name: title })).toBeInTheDocument();
      expect(screen.getByText(`${title} content`)).toBeInTheDocument();
    }
    // Navigation re-validates the session quietly, once per route change.
    await waitFor(() => expect(me).toHaveBeenCalledTimes(4));
    expect(screen.getByRole("navigation", { name: "Main" })).toBe(sidebar);
  });

  it("an expired session found during navigation still sends the person to sign in", async () => {
    vi.spyOn(api, "me").mockResolvedValueOnce(OWNER).mockRejectedValue(new ApiError("x", 401));
    const view = renderAt("/", "Dashboard");
    await screen.findByRole("navigation", { name: "Main" });
    nav.pathname = "/approvals";
    view.rerender(<WorkspaceShell><Page title="Approvals" /></WorkspaceShell>);
    await waitFor(() => expect(nav.replace).toHaveBeenCalledWith("/login?next=%2Fapprovals"));
    expect(screen.queryByRole("navigation", { name: "Main" })).not.toBeInTheDocument();
  });

  it("a transient failure during navigation keeps the page instead of blanking it", async () => {
    vi.spyOn(api, "me").mockResolvedValueOnce(OWNER).mockRejectedValue(new ApiError("x", 0));
    const view = renderAt("/", "Dashboard");
    const sidebar = await screen.findByRole("navigation", { name: "Main" });
    nav.pathname = "/settings";
    await act(async () => {
      view.rerender(<WorkspaceShell><Page title="Settings" /></WorkspaceShell>);
    });
    expect(screen.getByRole("navigation", { name: "Main" })).toBe(sidebar);
    expect(nav.replace).not.toHaveBeenCalled();
  });

  it("the first check shows the shell's frame with an announced status, not a bare page", async () => {
    let resolve: (user: typeof OWNER) => void = () => {};
    vi.spyOn(api, "me").mockImplementation(() => new Promise((done) => { resolve = done; }));
    renderAt("/", "Dashboard");
    const placeholder = screen.getByTestId("auth-loading");
    expect(placeholder.className).toContain("lg:grid-cols-[15rem_1fr]");
    expect(screen.getByRole("status")).toHaveTextContent("Checking your session…");
    await act(async () => resolve(OWNER));
    expect(screen.queryByTestId("auth-loading")).not.toBeInTheDocument();
  });

  it("a signed-out first visit is redirected without showing protected chrome", async () => {
    vi.spyOn(api, "me").mockRejectedValue(new ApiError("x", 401));
    renderAt("/settings", "Settings");
    await waitFor(() => expect(nav.replace).toHaveBeenCalledWith("/login?next=%2Fsettings"));
    expect(screen.queryByRole("navigation", { name: "Main" })).not.toBeInTheDocument();
  });
});

describe("route structure", () => {
  const root = path.resolve(__dirname, "..");
  const pages = (dir: string): string[] =>
    readdirSync(path.join(root, dir), { withFileTypes: true }).flatMap((entry) =>
      entry.isDirectory() ? pages(`${dir}/${entry.name}`) : entry.name === "page.tsx" ? [`${dir}/${entry.name}`] : []);

  it("every page that uses the shell lives under the persistent (workspace) layout", () => {
    const layout = readFileSync(path.join(root, "app/(workspace)/layout.tsx"), "utf8");
    expect(layout).toMatch(/<WorkspaceShell>/);
    for (const page of pages("app")) {
      const usesShell = /AppShell/.test(readFileSync(path.join(root, page), "utf8"));
      expect(usesShell, page).toBe(page.startsWith("app/(workspace)/"));
    }
  });

  it("sign-in stays outside the protected frame", () => {
    expect(existsSync(path.join(root, "app/login/page.tsx"))).toBe(true);
    expect(existsSync(path.join(root, "app/(workspace)/login"))).toBe(false);
  });

  it("no route-level loading boundary replaces the current page while the next one loads", () => {
    const loading = (dir: string): string[] =>
      readdirSync(path.join(root, dir), { withFileTypes: true }).flatMap((entry) =>
        entry.isDirectory() ? loading(`${dir}/${entry.name}`) : entry.name === "loading.tsx" ? [`${dir}/${entry.name}`] : []);
    expect(loading("app")).toEqual([]);
  });

  it("the guard no longer blanks the UI whenever the pathname changes", () => {
    const guard = readFileSync(path.join(root, "components/AuthGuard.tsx"), "utf8");
    expect(guard).toMatch(/if \(loading && !user\) return <ShellPlaceholder \/>/);
  });
});

describe("motion", () => {
  const css = readFileSync(path.resolve(__dirname, "../app/globals.css"), "utf8");

  it("page movement is small, brief and never fades the page", () => {
    const keyframes = /@keyframes reveal-in \{([^}]*\}[^}]*)\}/.exec(css)?.[1] ?? "";
    expect(keyframes).not.toMatch(/opacity/);
    expect(keyframes).toMatch(/translateY\(4px\)/);
    expect(css).toMatch(/\.animate-page-in, \.reveal \{ animation: reveal-in 160ms/);
    expect(css).toMatch(/\.animate-page-in \.reveal \{ animation: none; \}/);
  });

  it("interaction timings stay within the agreed ranges and respect reduced motion", () => {
    expect(css).toMatch(/transition-duration: 120ms/);
    expect(css).toMatch(/\[role="tab"\], \[aria-pressed\]\) \{ transition-duration: 150ms/);
    expect(css).toMatch(/prefers-reduced-motion: reduce[\s\S]*animation-duration: 0\.01ms !important; transition-duration: 0\.01ms !important/);
    const sidebar = readFileSync(path.resolve(__dirname, "../components/Sidebar.tsx"), "utf8");
    expect(sidebar.match(/duration-\[180ms\]/g)?.length).toBe(2);
    expect(sidebar).not.toMatch(/duration-300/);
  });
});
