import { readFileSync } from "node:fs";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { ApiError, CSRF_COOKIE, CSRF_HEADER } from "@/lib/api";
import { profile, profileInput, sectionEntry, type Fact, type FactSummary, type ProfileDetail } from "@/lib/profile";
import type { SubmissionGate } from "@/lib/review";

const auth = vi.hoisted(() => ({ role: "owner" as string | null }));

vi.mock("@/components/AuthGuard", () => ({
  useAuth: () => ({
    user: auth.role ? { id: "u1", email: "owner@example.com", role: auth.role } : null,
    loading: false,
    signOut: async () => {},
  }),
}));

import { ProfileView } from "@/components/profile/ProfileView";
import { FactsPanel } from "@/components/profile/FactsPanel";
import { SubmissionSwitchCard } from "@/components/settings/SubmissionSwitchCard";

const DRY_ON: SubmissionGate = { submission_mode: "dry_run", persisted_enabled: true, live: false };
const DRY_OFF: SubmissionGate = { submission_mode: "dry_run", persisted_enabled: false, live: false };
const LIVE_OFF: SubmissionGate = { submission_mode: "enabled", persisted_enabled: false, live: false };
const LIVE_ON: SubmissionGate = { submission_mode: "enabled", persisted_enabled: true, live: true };

function detail(overrides: Partial<ProfileDetail> = {}): ProfileDetail {
  return {
    id: "p1",
    full_name: "Demo Candidate",
    headline: "Cloud engineer",
    email: "demo@example.com",
    phone: "555-0100",
    location_city: "Austin",
    location_state: "TX",
    location_country: "US",
    work_authorization: "SYNTHETIC-AUTH-VALUE",
    willing_to_relocate: false,
    target_roles: ["Cloud Engineer"],
    min_salary: 100000,
    max_salary: 150000,
    links: { github: "https://example.com/demo" },
    version: 3,
    updated_at: "2026-11-03T00:00:00Z",
    experiences: [{ id: "e1", title: "Engineer", company_name: "Demo Co", start_date: "2020-01-01", end_date: null, is_current: true }],
    educations: [],
    certifications: [],
    skills: [],
    projects: [],
    ...overrides,
  };
}

function fact(overrides: Partial<Fact> = {}): Fact {
  return {
    id: "f1",
    fact_type: "skill",
    text: "Runs AWS infrastructure",
    structured: {},
    source_table: "experiences",
    source_id: "e1",
    status: "draft",
    confidence: null,
    usage_count: 0,
    verified_by: null,
    verified_at: null,
    updated_at: "2026-11-03T00:00:00Z",
    ...overrides,
  };
}

const SUMMARY: FactSummary = { total: 1, by_status: { draft: 1 }, citable: 0, never_used: 1, draft_awaiting_verification: 1 };

beforeEach(() => {
  auth.role = "owner";
});

afterEach(() => {
  vi.unstubAllGlobals();
  document.cookie = `${CSRF_COOKIE}=; expires=Thu, 01 Jan 1970 00:00:00 GMT`;
});

// --------------------------------------------------------------- settings
describe("submission switch", () => {
  it("never implies the persisted switch makes dry-run live", async () => {
    vi.spyOn(profile, "submissionGate").mockResolvedValue(DRY_ON);
    render(<SubmissionSwitchCard />);
    const gate = await screen.findByTestId("settings-gate");
    expect(gate).toHaveTextContent("Dry-run · never sends");
    expect(screen.getByTestId("gate-summary")).toHaveTextContent(/never sends email/);
    expect(gate).not.toHaveTextContent(/sends a real email/);
  });

  it("turning on in dry-run is confirmed, says nothing is sent, and re-reads the server", async () => {
    const read = vi.spyOn(profile, "submissionGate").mockResolvedValueOnce(DRY_OFF).mockResolvedValue(DRY_ON);
    const write = vi.spyOn(profile, "setSubmissionEnabled").mockResolvedValue({ submission_enabled: true });
    render(<SubmissionSwitchCard />);
    fireEvent.click(await screen.findByRole("button", { name: "Turn submission switch on…" }));
    const dialog = screen.getByRole("dialog", { name: "Turn the submission switch on?" });
    expect(dialog).toHaveTextContent(/nothing is sent/);
    expect(dialog).not.toHaveTextContent(/sends a real email/);
    const confirm = within(dialog).getByRole("button", { name: "Turn on" });
    expect(confirm).toBeDisabled();
    fireEvent.click(within(dialog).getByRole("checkbox"));
    fireEvent.click(confirm);
    fireEvent.click(confirm);
    expect(await screen.findByText(/switch is on\. The state above was re-read/)).toBeInTheDocument();
    expect(write).toHaveBeenCalledTimes(1);
    expect(write).toHaveBeenCalledWith(true);
    expect(read).toHaveBeenCalledTimes(2);
  });

  it("in live mode the dialog warns that a confirmed Submit sends real email", async () => {
    vi.spyOn(profile, "submissionGate").mockResolvedValue(LIVE_OFF);
    render(<SubmissionSwitchCard />);
    fireEvent.click(await screen.findByRole("button", { name: "Turn submission switch on…" }));
    const dialog = screen.getByRole("dialog");
    expect(dialog).toHaveTextContent(/sends a real email/);
    expect(dialog).toHaveTextContent(/Nothing is sent automatically/);
  });

  it("turning off is offered when on, and a refusal stays in the dialog", async () => {
    vi.spyOn(profile, "submissionGate").mockResolvedValue(LIVE_ON);
    vi.spyOn(profile, "setSubmissionEnabled").mockRejectedValue(new ApiError("x", 403));
    render(<SubmissionSwitchCard />);
    fireEvent.click(await screen.findByRole("button", { name: "Turn submission switch off…" }));
    const dialog = screen.getByRole("dialog", { name: "Turn the submission switch off?" });
    fireEvent.click(within(dialog).getByRole("checkbox"));
    fireEvent.click(within(dialog).getByRole("button", { name: "Turn off" }));
    expect(await within(dialog).findByRole("alert")).toBeInTheDocument();
  });

  it("reviewers see the state but no control", async () => {
    auth.role = "reviewer";
    vi.spyOn(profile, "submissionGate").mockResolvedValue(DRY_OFF);
    render(<SubmissionSwitchCard />);
    expect(await screen.findByTestId("reviewer-read-only")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Turn submission switch/ })).not.toBeInTheDocument();
  });

  it("a load failure offers retry", async () => {
    vi.spyOn(profile, "submissionGate").mockRejectedValueOnce(new ApiError("x", 500)).mockResolvedValue(DRY_OFF);
    render(<SubmissionSwitchCard />);
    fireEvent.click(await screen.findByRole("button", { name: "Try again" }));
    expect(await screen.findByTestId("settings-gate")).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------- profile
describe("profile", () => {
  it("an edit sends the complete current record and version, so nothing is reset", async () => {
    vi.spyOn(profile, "get").mockResolvedValue(detail());
    vi.spyOn(profile, "facts").mockResolvedValue([]);
    vi.spyOn(profile, "factSummary").mockResolvedValue(SUMMARY);
    const update = vi.spyOn(profile, "update").mockResolvedValue(detail());
    render(<ProfileView />);
    fireEvent.click(await screen.findByRole("button", { name: "Edit basics" }));
    fireEvent.change(screen.getByLabelText("Headline"), { target: { value: "Platform engineer" } });
    fireEvent.change(screen.getByRole("textbox", { name: "Target roles" }), { target: { value: "Cloud Engineer, SRE" } });
    fireEvent.click(screen.getByRole("button", { name: "Save profile" }));
    await waitFor(() => expect(update).toHaveBeenCalledTimes(1));
    const [input, version] = update.mock.calls[0]!;
    expect(version).toBe(3);
    expect(input).toMatchObject({ headline: "Platform engineer", target_roles: ["Cloud Engineer", "SRE"], min_salary: 100000, max_salary: 150000, work_authorization: "SYNTHETIC-AUTH-VALUE", links: { github: "https://example.com/demo" } });
    expect(input).not.toHaveProperty("id");
    expect(input).not.toHaveProperty("experiences");
    expect(await screen.findByText(/Profile saved/)).toBeInTheDocument();
  });

  it("a stale version is explained", async () => {
    vi.spyOn(profile, "get").mockResolvedValue(detail());
    vi.spyOn(profile, "facts").mockResolvedValue([]);
    vi.spyOn(profile, "factSummary").mockResolvedValue(SUMMARY);
    vi.spyOn(profile, "update").mockRejectedValue(new ApiError("x", 409, { detail: "version mismatch" }));
    render(<ProfileView />);
    fireEvent.click(await screen.findByRole("button", { name: "Edit basics" }));
    fireEvent.click(screen.getByRole("button", { name: "Save profile" }));
    expect(await screen.findByText(/changed since you opened it/)).toBeInTheDocument();
  });

  it("reviewers see no contact details, work authorization or edit controls", async () => {
    auth.role = "reviewer";
    vi.spyOn(profile, "get").mockResolvedValue(detail());
    vi.spyOn(profile, "facts").mockResolvedValue([fact()]);
    vi.spyOn(profile, "factSummary").mockResolvedValue(SUMMARY);
    render(<ProfileView />);
    expect(await screen.findByText("Demo Candidate")).toBeInTheDocument();
    expect(screen.queryByText("demo@example.com")).not.toBeInTheDocument();
    expect(screen.queryByText(/Work authorization/)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Edit basics" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Suggest facts/ })).not.toBeInTheDocument();
    expect(await screen.findByText("Runs AWS infrastructure")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Verify…" })).not.toBeInTheDocument();
  });

  it("the owner never sees the work authorization value outside the edit form", async () => {
    vi.spyOn(profile, "get").mockResolvedValue(detail());
    vi.spyOn(profile, "facts").mockResolvedValue([]);
    vi.spyOn(profile, "factSummary").mockResolvedValue(SUMMARY);
    render(<ProfileView />);
    expect(await screen.findByText(/Recorded \(never sent to a model\)/)).toBeInTheDocument();
    expect(screen.queryByText("SYNTHETIC-AUTH-VALUE")).not.toBeInTheDocument();
  });

  it("suggesting facts reports the drafts and refreshes the facts list", async () => {
    vi.spyOn(profile, "get").mockResolvedValue(detail());
    const list = vi.spyOn(profile, "facts").mockResolvedValue([]);
    vi.spyOn(profile, "factSummary").mockResolvedValue(SUMMARY);
    vi.spyOn(profile, "suggestFacts").mockResolvedValue([fact(), fact({ id: "f2" })]);
    render(<ProfileView />);
    fireEvent.click(await screen.findByRole("button", { name: "Suggest facts from Engineer — Demo Co" }));
    expect(await screen.findByText(/2 draft facts suggested\. Verify each one/)).toBeInTheDocument();
    await waitFor(() => expect(list).toHaveBeenCalledTimes(2));
  });

  it("no profile yet is an empty state, not an error", async () => {
    vi.spyOn(profile, "get").mockRejectedValue(new ApiError("x", 404, { detail: "no profile exists yet" }));
    render(<ProfileView />);
    expect(await screen.findByText("No profile yet")).toBeInTheDocument();
  });
});

// ------------------------------------------------------------------ facts
describe("facts", () => {
  it("verification is confirmed, sent once, and re-read", async () => {
    const list = vi.spyOn(profile, "facts").mockResolvedValueOnce([fact()]).mockResolvedValue([]);
    vi.spyOn(profile, "factSummary").mockResolvedValue(SUMMARY);
    const verify = vi.spyOn(profile, "verifyFact").mockResolvedValue(fact({ status: "verified" }));
    render(<FactsPanel isOwner />);
    fireEvent.click(await screen.findByRole("button", { name: "Verify…" }));
    const dialog = screen.getByRole("dialog", { name: "Verify this fact?" });
    expect(dialog).toHaveTextContent("Runs AWS infrastructure");
    fireEvent.click(within(dialog).getByRole("checkbox"));
    fireEvent.click(within(dialog).getByRole("button", { name: "Verify" }));
    expect(await screen.findByText(/Verified\. The list was re-read/)).toBeInTheDocument();
    expect(verify).toHaveBeenCalledWith("f1");
    expect(list).toHaveBeenCalledTimes(2);
  });

  it("a verified fact can only be returned to draft or retired, not edited", async () => {
    vi.spyOn(profile, "facts").mockResolvedValue([fact({ status: "verified", verified_at: "2026-11-03T00:00:00Z" })]);
    vi.spyOn(profile, "factSummary").mockResolvedValue(SUMMARY);
    render(<FactsPanel isOwner />);
    expect(await screen.findByRole("button", { name: "Return to draft…" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Edit" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Verify…" })).not.toBeInTheDocument();
  });

  it("filters by status through the API", async () => {
    const list = vi.spyOn(profile, "facts").mockResolvedValue([]);
    vi.spyOn(profile, "factSummary").mockResolvedValue(SUMMARY);
    render(<FactsPanel isOwner />);
    expect(await screen.findByText("Nothing is waiting for verification.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Verified" }));
    await waitFor(() => expect(list).toHaveBeenLastCalledWith("verified"));
    fireEvent.click(screen.getByRole("button", { name: "All" }));
    await waitFor(() => expect(list).toHaveBeenLastCalledWith(undefined));
  });

  it("editing a draft saves only the text", async () => {
    vi.spyOn(profile, "facts").mockResolvedValue([fact()]);
    vi.spyOn(profile, "factSummary").mockResolvedValue(SUMMARY);
    const edit = vi.spyOn(profile, "editFact").mockResolvedValue(fact({ text: "Runs AWS and GCP" }));
    render(<FactsPanel isOwner />);
    fireEvent.click(await screen.findByRole("button", { name: "Edit" }));
    fireEvent.change(screen.getByLabelText("Fact text"), { target: { value: "Runs AWS and GCP" } });
    fireEvent.click(screen.getByRole("button", { name: "Save draft" }));
    expect(await screen.findByText("Draft saved.")).toBeInTheDocument();
    expect(edit).toHaveBeenCalledWith("f1", "Runs AWS and GCP");
  });

  it("evidence is listed and added without any attribution field", async () => {
    vi.spyOn(profile, "facts").mockResolvedValue([fact()]);
    vi.spyOn(profile, "factSummary").mockResolvedValue(SUMMARY);
    vi.spyOn(profile, "evidence").mockResolvedValueOnce([]).mockResolvedValue([{ id: "v1", kind: "attestation", url: null, note: "Synthetic note", added_by: "u1", created_at: "2026-11-03T00:00:00Z" }]);
    const add = vi.spyOn(profile, "addEvidence").mockResolvedValue({ id: "v1", kind: "attestation", url: null, note: "Synthetic note", added_by: "u1", created_at: "2026-11-03T00:00:00Z" });
    render(<FactsPanel isOwner />);
    fireEvent.click(await screen.findByRole("button", { name: "Evidence" }));
    expect(await screen.findByText("No evidence attached.")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Note"), { target: { value: "Synthetic note" } });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    expect(await screen.findByText(/Synthetic note/)).toBeInTheDocument();
    expect(add).toHaveBeenCalledWith("f1", { kind: "attestation", url: "", note: "Synthetic note" });
  });
});

// ----------------------------------------------------------------- client
describe("profile client", () => {
  function response(body: unknown, status = 200): Response {
    return { ok: status >= 200 && status < 300, status, text: async () => JSON.stringify(body) } as Response;
  }

  it("verify sends no attribution and carries CSRF", async () => {
    document.cookie = `${CSRF_COOKIE}=tok`;
    const fetch = vi.fn().mockResolvedValue(response(fact({ status: "verified" })));
    vi.stubGlobal("fetch", fetch);
    await profile.verifyFact("f/1");
    const [url, init] = fetch.mock.calls[0]!;
    expect(String(url)).toContain("/profile/facts/f%2F1/verify");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body)).toEqual({});
    expect(init.headers[CSRF_HEADER]).toBe("tok");
  });

  it("evidence omits added_by and blanks become null", async () => {
    const fetch = vi.fn().mockResolvedValue(response({}));
    vi.stubGlobal("fetch", fetch);
    await profile.addEvidence("f1", { kind: "url", url: " https://example.com ", note: "  " });
    expect(JSON.parse(fetch.mock.calls[0]![1].body)).toEqual({ kind: "url", url: "https://example.com", note: null });
  });

  it("the switch is a PUT of one boolean", async () => {
    const fetch = vi.fn().mockResolvedValue(response({ submission_enabled: false }));
    vi.stubGlobal("fetch", fetch);
    await profile.setSubmissionEnabled(false);
    const [url, init] = fetch.mock.calls[0]!;
    expect(String(url)).toContain("/settings/submission-enabled");
    expect(init.method).toBe("PUT");
    expect(JSON.parse(init.body)).toEqual({ enabled: false });
  });

  it("profileInput keeps exactly the editable fields", () => {
    expect(Object.keys(profileInput(detail())).sort()).toEqual([
      "email", "full_name", "headline", "links", "location_city", "location_country", "location_state",
      "max_salary", "min_salary", "phone", "target_roles", "willing_to_relocate", "work_authorization",
    ]);
  });

  it("section entries read naturally", () => {
    expect(sectionEntry("experiences", { id: "e", title: "SRE", company_name: "Co", start_date: "2020-01-01", end_date: null, is_current: true }).title).toBe("SRE — Co");
    expect(sectionEntry("projects", { id: "p", name: "Infra", role: "Lead", summary: "Built it" })).toMatchObject({ title: "Infra", detail: "Lead · Built it" });
  });
});

describe("no placeholders or demo data", () => {
  it("profile and settings pages are real", () => {
    const root = path.resolve(__dirname, "..");
    for (const file of ["app/profile/page.tsx", "app/settings/page.tsx", "components/profile/ProfileView.tsx", "components/profile/FactsPanel.tsx", "components/settings/SubmissionSwitchCard.tsx", "lib/profile.ts"]) {
      expect(readFileSync(path.join(root, file), "utf8"), file).not.toMatch(/demo-data|DemoDataNotice|PlaceholderPage/);
    }
  });
});
