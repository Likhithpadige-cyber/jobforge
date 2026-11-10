import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const auth = vi.hoisted(() => ({
  user: { id: "u1", email: "demo.candidate@example.com", role: "owner" } as
    | { id: string; email: string; role: string }
    | null,
}));
vi.mock("@/components/AuthGuard", () => ({
  useAuth: () => ({ user: auth.user, loading: false, signOut: async () => {} }),
}));

import {
  JobImportPanel,
  MAX_IMPORT_BYTES,
  fileFormat,
  pastedRequest,
  validatePosting,
} from "@/components/JobImportPanel";
import { ApiError, api, type ApiSource } from "@/lib/api";

const SOURCES: ApiSource[] = [
  { id: "s1", name: "Manual import", slug: "manual", domain: "manual.invalid", enabled: true },
];
const DESCRIPTION =
  "We need a Linux administrator to run RHEL fleets, automate with Ansible and join the on-call rotation.";

function fill(label: RegExp, value: string) {
  fireEvent.change(screen.getByLabelText(label), { target: { value } });
}

function openForm() {
  render(<JobImportPanel sources={SOURCES} onImported={vi.fn()} />);
  fireEvent.click(screen.getByRole("button", { name: "Add a job" }));
}

describe("job import helpers", () => {
  it("reports every missing or malformed field", () => {
    const problems = validatePosting({ title: "", company: "", url: "ftp://x", location: "", description: "short" });
    expect(problems).toHaveLength(4);
    expect(problems.join(" ")).toMatch(/title/);
    expect(problems.join(" ")).toMatch(/http/);
  });

  it("sends exactly what the owner typed, trimmed, to the pasted-posting endpoint body", () => {
    expect(
      pastedRequest({ title: " Engineer ", company: "Co", url: "https://jobs.example/1", location: "", description: DESCRIPTION }),
    ).toEqual({ title: "Engineer", company_name: "Co", url: "https://jobs.example/1", description: DESCRIPTION });
  });

  it("only accepts CSV and JSON files", () => {
    expect(fileFormat("jobs.CSV")).toBe("csv");
    expect(fileFormat("jobs.json")).toBe("json");
    expect(fileFormat("jobs.pdf")).toBeNull();
  });
});

describe("JobImportPanel", () => {
  beforeEach(() => {
    auth.user = { id: "u1", email: "demo.candidate@example.com", role: "owner" };
    vi.restoreAllMocks();
  });

  it("is not shown to reviewers", () => {
    auth.user = { id: "u2", email: "demo.reviewer@example.com", role: "reviewer" };
    render(<JobImportPanel sources={SOURCES} onImported={vi.fn()} />);
    expect(screen.queryByRole("button", { name: "Add a job" })).toBeNull();
  });

  it("shows validation errors and sends nothing for an incomplete posting", async () => {
    const spy = vi.spyOn(api, "importPastedJob");
    openForm();
    fireEvent.click(screen.getByRole("button", { name: "Import" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/Enter the job title/);
    expect(spy).not.toHaveBeenCalled();
  });

  it("imports a pasted posting, reports the outcome and refreshes the feed", async () => {
    const onImported = vi.fn();
    const spy = vi.spyOn(api, "importPastedJob").mockResolvedValue({
      source_slug: "owner-pasted", total: 1, created: 1, duplicates: 0, updated: 0, rejected: 0,
      outcomes: [{ url: "https://jobs.example/1", result: "created", job_id: "j1", canonical_id: null, reason: null, warnings: [] }],
    });
    render(<JobImportPanel sources={SOURCES} onImported={onImported} />);
    fireEvent.click(screen.getByRole("button", { name: "Add a job" }));
    fill(/Job title/, "Linux Administrator");
    fill(/Company/, "Example Health");
    fill(/Posting link/, "https://jobs.example/1");
    fill(/Job description/, DESCRIPTION);
    fireEvent.click(screen.getByRole("button", { name: "Import" }));
    await waitFor(() => expect(onImported).toHaveBeenCalled());
    expect(spy).toHaveBeenCalledWith(expect.objectContaining({ title: "Linux Administrator", company_name: "Example Health" }));
    expect(screen.getByRole("status")).toHaveTextContent("1 added");
    expect(screen.getByRole("link", { name: "Open job" })).toHaveAttribute("href", "/job-feed/j1");
  });

  it("shows a server refusal without refreshing the feed", async () => {
    const onImported = vi.fn();
    vi.spyOn(api, "importPastedJob").mockRejectedValue(new ApiError("Request failed: 422", 422, { detail: "row 1: missing required field(s): url" }));
    render(<JobImportPanel sources={SOURCES} onImported={onImported} />);
    fireEvent.click(screen.getByRole("button", { name: "Add a job" }));
    fill(/Job title/, "Linux Administrator");
    fill(/Company/, "Example Health");
    fill(/Posting link/, "https://jobs.example/1");
    fill(/Job description/, DESCRIPTION);
    fireEvent.click(screen.getByRole("button", { name: "Import" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/missing required field/);
    expect(onImported).not.toHaveBeenCalled();
  });

  it("explains a site-policy refusal for bulk files and points to pasting", async () => {
    vi.spyOn(api, "importJobs").mockRejectedValue(
      new ApiError("Request failed: 403", 403, { detail: "no site_policies row for 'jobs.example'; unknown domains are blocked" }),
    );
    openForm();
    fireEvent.click(screen.getByLabelText(/Upload CSV or JSON/));
    const input = screen.getByLabelText(/File \(.csv or .json/) as HTMLInputElement;
    const csv = new File(["title,url\nRole,https://jobs.example/1\n"], "jobs.csv");
    // jsdom lacks Blob.text(); every supported browser has it.
    Object.defineProperty(csv, "text", { value: async () => "title,url\nRole,https://jobs.example/1\n" });
    fireEvent.change(input, { target: { files: [csv] } });
    fireEvent.click(screen.getByRole("button", { name: "Import" }));
    const alert = await screen.findByText(/reviewed site policy/);
    expect(alert.closest("[role=alert]")).not.toBeNull();
    expect(alert).toHaveTextContent(/paste it instead/);
  });

  it("rejects unsupported and oversized files before uploading", async () => {
    const spy = vi.spyOn(api, "importJobs");
    openForm();
    fireEvent.click(screen.getByLabelText(/Upload CSV or JSON/));
    const input = screen.getByLabelText(/File \(.csv or .json/) as HTMLInputElement;
    const big = new File(["x"], "jobs.csv", { type: "text/csv" });
    Object.defineProperty(big, "size", { value: MAX_IMPORT_BYTES + 1 });
    fireEvent.change(input, { target: { files: [big] } });
    fireEvent.click(screen.getByRole("button", { name: "Import" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/5 MB/);
    fireEvent.change(input, { target: { files: [new File(["x"], "jobs.pdf")] } });
    fireEvent.click(screen.getByRole("button", { name: "Import" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/Only .csv and .json/);
    expect(spy).not.toHaveBeenCalled();
  });
});
