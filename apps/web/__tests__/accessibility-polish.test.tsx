import { readFileSync } from "node:fs";
import path from "node:path";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { initialsFor } from "@/lib/initials";
import { asSentence } from "@/lib/review";

const auth = vi.hoisted(() => ({ user: { id: "u1", email: "demo.candidate@example.com", role: "owner" } as { id: string; email: string; role: string } | null }));

vi.mock("@/components/AuthGuard", () => ({
  AuthGuard: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  useAuth: () => ({ user: auth.user, loading: false, signOut: async () => {} }),
}));

vi.mock("@/components/operations/OperationsProvider", () => ({
  OperationsProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  useOperations: () => ({ enabled: false, openAlerts: null, pause: null }),
}));

vi.mock("@/components/operations/GenerationPauseBanner", () => ({ GenerationPauseBanner: () => null }));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
  usePathname: () => "/settings",
}));

import { AppShell } from "@/components/AppShell";
import { WorkspaceShell } from "@/components/WorkspaceShell";
import { Header } from "@/components/Header";
import { DecisionForm } from "@/components/review/DecisionForm";
import { ReviewLoading, SectionCard } from "@/components/review/ReviewStates";

beforeEach(() => {
  auth.user = { id: "u1", email: "demo.candidate@example.com", role: "owner" };
});

// --------------------------------------------------------------- initials
describe("initials", () => {
  it.each([
    ["demo.candidate@example.com", "DC"],
    ["demo.reviewer@example.com", "DR"],
    ["jane_q_public@example.com", "JP"],
    ["alex@example.com", "A"],
    ["ana-maria+jobs@example.com", "AJ"],
    ["émile.zola@example.com", "ÉZ"],
    ["  x.y  @example.com", "XY"],
  ])("%s → %s", (email, expected) => {
    expect(initialsFor({ email })).toBe(expected);
  });

  it.each([[null], [undefined], [{}], [{ email: "" }], [{ email: "@example.com" }], [{ email: "1234@example.com" }], [{ email: "..._@example.com" }]])(
    "no usable letters → null (%o)",
    (user) => {
      expect(initialsFor(user as never)).toBeNull();
    },
  );
});

// ----------------------------------------------------------------- header
describe("account avatar", () => {
  it("shows the signed-in owner's own initials, linked to their profile", () => {
    render(<Header title="Settings" />);
    const avatar = screen.getByTestId("account-avatar");
    expect(avatar).toHaveTextContent("DC");
    expect(avatar.tagName).toBe("A");
    expect(avatar).toHaveAttribute("href", "/profile");
    expect(avatar).toHaveAccessibleName("DC. Your profile. Signed in as demo.candidate@example.com");
  });

  it("a reviewer sees their own initials and is not sent to the candidate's profile", () => {
    auth.user = { id: "u2", email: "demo.reviewer@example.com", role: "reviewer" };
    render(<Header title="Settings" />);
    const avatar = screen.getByTestId("account-avatar");
    expect(avatar).toHaveTextContent("DR");
    expect(avatar.tagName).toBe("SPAN");
    expect(avatar).toHaveAccessibleName("Signed in as demo.reviewer@example.com (reviewer)");
  });

  it("no usable name shows an icon, never someone else's initials", () => {
    auth.user = { id: "u3", email: "1234@example.com", role: "owner" };
    render(<Header title="Settings" />);
    const avatar = screen.getByTestId("account-avatar");
    expect(avatar.textContent).toBe("");
    expect(avatar.querySelector("svg")).not.toBeNull();
  });

  it("nothing is shown before the session is known", () => {
    auth.user = null;
    render(<Header title="Settings" />);
    expect(screen.queryByTestId("account-avatar")).not.toBeInTheDocument();
  });

  it("no hardcoded initials or names remain", () => {
    const source = readFileSync(path.resolve(__dirname, "../components/Header.tsx"), "utf8");
    expect(source).not.toMatch(/>\s*LS\s*</);
    expect(source).not.toMatch(/Likhith/);
  });

  it("makes no API request", () => {
    const fetch = vi.fn();
    vi.stubGlobal("fetch", fetch);
    render(<Header title="Settings" />);
    expect(fetch).not.toHaveBeenCalled();
    vi.unstubAllGlobals();
  });
});

// -------------------------------------------------------------- navigation
describe("mobile navigation drawer", () => {
  it("is a modal dialog that takes focus, traps Tab, closes on Escape and restores focus", () => {
    render(<WorkspaceShell><AppShell title="Settings"><p>Body</p></AppShell></WorkspaceShell>);
    const opener = screen.getByRole("button", { name: "Open navigation" });
    expect(opener).toHaveAttribute("aria-haspopup", "dialog");
    opener.focus();
    fireEvent.click(opener);
    const drawer = screen.getByRole("dialog", { name: "Navigation" });
    expect(drawer).toHaveAttribute("aria-modal", "true");
    expect(drawer.contains(document.activeElement)).toBe(true);

    const focusable = within(drawer).getAllByRole("link");
    focusable[focusable.length - 1]!.focus();
    fireEvent.keyDown(document, { key: "Tab" });
    // Tab from the last control wraps rather than leaving the dialog.
    expect(drawer.contains(document.activeElement)).toBe(true);

    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("dialog", { name: "Navigation" })).not.toBeInTheDocument();
    expect(document.activeElement).toBe(opener);
  });

  it("the sidebar never claims something is waiting without data", () => {
    const source = readFileSync(path.resolve(__dirname, "../components/Sidebar.tsx"), "utf8");
    expect(source).not.toMatch(/Review what is waiting for your approval/);
  });
});

// ------------------------------------------------------------ structure
describe("structure and announcements", () => {
  it("section cards are h2 regions named by their heading", () => {
    render(<SectionCard title="Facts"><p>x</p></SectionCard>);
    const heading = screen.getByRole("heading", { level: 2, name: "Facts" });
    expect(screen.getByRole("region", { name: "Facts" })).toContainElement(heading);
  });

  it("loading states are announced with text, not only a skeleton", () => {
    render(<ReviewLoading label="Loading facts" />);
    expect(screen.getByRole("status", { name: "Loading facts" })).toHaveTextContent("Loading facts…");
  });

  it("the required change note is marked required and the disabled button explains itself", () => {
    render(<DecisionForm subject="cover letter" canApprove canRequestChanges blockedReason={null} approveAcknowledgement="ok" changesEffect="Returned for rework." onApprove={async () => {}} onRequestChanges={async () => {}} />);
    expect(screen.getByLabelText("What should change? (required)")).toHaveAttribute("aria-required", "true");
    const button = screen.getByRole("button", { name: "Request changes…" });
    expect(button).toBeDisabled();
    expect(button).toHaveAccessibleDescription("Write what should change to enable this.");
    fireEvent.change(screen.getByLabelText("What should change? (required)"), { target: { value: "Shorter" } });
    expect(button).toBeEnabled();
    expect(button).not.toHaveAttribute("aria-describedby");
  });

  it("server reasons read as sentences", () => {
    expect(asSentence("the current cover-letter approval is already approved")).toBe("The current cover-letter approval is already approved.");
    expect(asSentence("Already done.")).toBe("Already done.");
    expect(asSentence("  ")).toBe("");
  });
});

// --------------------------------------------------------------- contrast
describe("colour contrast tokens", () => {
  const luminance = (hex: string) => {
    const [r, g, b] = [1, 3, 5].map((i) => {
      const c = parseInt(hex.slice(i, i + 2), 16) / 255;
      return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
    });
    return 0.2126 * r! + 0.7152 * g! + 0.0722 * b!;
  };
  const ratio = (a: string, b: string) => {
    const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
    return (hi! + 0.05) / (lo! + 0.05);
  };
  const css = readFileSync(path.resolve(__dirname, "../app/globals.css"), "utf8");
  const token = (name: string) => new RegExp(`--jf-${name}:\\s*(#[0-9A-Fa-f]{6})`).exec(css)![1]!;
  it.each([
    ["cream", "midnight"], ["cream", "deep-teal"],
    ["aqua", "midnight"], ["aqua", "deep-teal"],
    ["midnight", "orange"], ["midnight", "cream"], ["deep-teal", "cream"],
  ])("%s text on %s meets 4.5:1", (foreground, background) => {
    expect(ratio(token(foreground), token(background))).toBeGreaterThanOrEqual(4.5);
  });
  it.each([
    ["aqua", "midnight"], ["aqua", "deep-teal"],
    ["cream", "orange"], ["orange", "midnight"],
    ["deep-teal", "cream"], ["orange", "cream"], ["muted-teal", "cream"],
  ])("%s control boundary on %s meets 3:1", (foreground, background) => {
    expect(ratio(token(foreground), token(background))).toBeGreaterThanOrEqual(3);
  });
  it("does not treat inaccessible palette combinations as body-text pairings", () => {
    expect(ratio(token("cream"), token("orange"))).toBeLessThan(4.5);
    expect(ratio(token("cream"), token("muted-teal"))).toBeLessThan(4.5);
    expect(ratio(token("aqua"), token("cream"))).toBeLessThan(3);
  });
});
