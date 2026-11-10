import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { MasterResumeImport, parseMasterText, toMasterData } from "@/components/profile/MasterResumeImport";
import { ApiError } from "@/lib/api";
import { profile, type MasterResumeData } from "@/lib/profile";

const RESUME = `Jordan Example
Linux administrator focused on automation.

## Experience
- Administered 180 RHEL servers with Ansible.
• Built systemd units for 14 services.

Technical Skills:
1. Bash, Ansible, Git

EDUCATION
BS Information Technology, Example State University`;

describe("parseMasterText", () => {
  it("assigns lines to sections by recognised headings and strips bullets", () => {
    const lines = parseMasterText(RESUME);
    expect(lines.map((l) => [l.section, l.text])).toEqual([
      ["summary", "Jordan Example"],
      ["summary", "Linux administrator focused on automation."],
      ["experience", "Administered 180 RHEL servers with Ansible."],
      ["experience", "Built systemd units for 14 services."],
      ["skills", "Bash, Ansible, Git"],
      ["education", "BS Information Technology, Example State University"],
    ]);
  });

  it("builds the import body with only non-empty sections in a fixed order", () => {
    const data = toMasterData(parseMasterText(RESUME), "resume.txt");
    expect(data.schema_version).toBe(1);
    expect(data.source_label).toBe("resume.txt");
    expect(data.sections.map((s) => s.name)).toEqual(["summary", "experience", "skills", "education"]);
  });
});

describe("MasterResumeImport", () => {
  beforeEach(() => vi.restoreAllMocks());

  function openAndPreview(text = RESUME) {
    render(<MasterResumeImport profileId="p1" onSaved={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Import master resume" }));
    fireEvent.change(screen.getByLabelText("Resume text"), { target: { value: text } });
    fireEvent.click(screen.getByRole("button", { name: "Preview lines" }));
  }

  it("refuses PDF and other files with paste guidance", async () => {
    const spy = vi.spyOn(profile, "extractDocxText");
    render(<MasterResumeImport profileId="p1" onSaved={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Import master resume" }));
    fireEvent.change(screen.getByLabelText(/Or open a file/), { target: { files: [new File(["x"], "resume.pdf")] } });
    expect(await screen.findByRole("alert")).toHaveTextContent(/For a PDF, copy its text and paste it/);
    expect(spy).not.toHaveBeenCalled();
  });

  it("reads a .docx through the API into the editable text, saving nothing", async () => {
    const extract = vi.spyOn(profile, "extractDocxText").mockResolvedValue({ text: "Jordan Example\n## Experience\n- Ran Linux hosts.", lines: 3, source: "docx" });
    const save = vi.spyOn(profile, "importMasterResume");
    render(<MasterResumeImport profileId="p1" onSaved={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Import master resume" }));
    const file = new File(["PK"], "resume.docx");
    fireEvent.change(screen.getByLabelText(/Or open a file/), { target: { files: [file] } });
    await waitFor(() => expect(screen.getByLabelText("Resume text")).toHaveValue("Jordan Example\n## Experience\n- Ran Linux hosts."));
    expect(extract).toHaveBeenCalledWith("p1", file);
    expect(save).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Preview lines" }));
    expect(screen.getByLabelText("Text of line 2")).toHaveValue("Ran Linux hosts.");
  });

  it("shows the server's reason when a .docx cannot be read", async () => {
    vi.spyOn(profile, "extractDocxText").mockRejectedValue(new ApiError("x", 422, { detail: "the document is compressed suspiciously well" }));
    render(<MasterResumeImport profileId="p1" onSaved={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Import master resume" }));
    fireEvent.change(screen.getByLabelText(/Or open a file/), { target: { files: [new File(["PK"], "resume.docx")] } });
    expect(await screen.findByRole("alert")).toHaveTextContent("compressed suspiciously");
  });

  it("lets the owner correct sections, edit and remove lines before saving", async () => {
    const onSaved = vi.fn();
    const spy = vi.spyOn(profile, "importMasterResume").mockResolvedValue({ resume_id: "r1", profile_id: "p1", version: 2, kind: "base" });
    render(<MasterResumeImport profileId="p1" onSaved={onSaved} />);
    fireEvent.click(screen.getByRole("button", { name: "Import master resume" }));
    fireEvent.change(screen.getByLabelText("Resume text"), { target: { value: RESUME } });
    fireEvent.click(screen.getByRole("button", { name: "Preview lines" }));
    fireEvent.click(screen.getByRole("button", { name: "Remove line 1" }));
    fireEvent.change(screen.getByLabelText("Section for line 2"), { target: { value: "experience" } });
    fireEvent.change(screen.getByLabelText("Text of line 3"), { target: { value: "Administered 180 RHEL 8 servers." } });
    fireEvent.click(screen.getByRole("button", { name: "Save master resume" }));
    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    const body: MasterResumeData = spy.mock.calls[0]![1];
    expect(body.sections.find((s) => s.name === "summary")).toBeUndefined();
    expect(body.sections.find((s) => s.name === "experience")?.entries[1]).toEqual({ text: "Administered 180 RHEL 8 servers." });
    expect(String(onSaved.mock.calls[0]?.[0])).toMatch(/not evidence/);
  });

  it("shows the server's refusal", async () => {
    vi.spyOn(profile, "importMasterResume").mockRejectedValue(new ApiError("Request failed: 404", 404, { detail: "candidate or evidence not found" }));
    openAndPreview();
    fireEvent.click(screen.getByRole("button", { name: "Save master resume" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/candidate or evidence not found/);
  });
});
