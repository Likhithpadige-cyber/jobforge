import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const download = vi.hoisted(() => vi.fn());
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, downloadArtifactBytes: download };
});

import { ResumeExportPanel, pickCurrent, safeFilename, sha256Hex } from "@/components/ResumeExportPanel";
import { ApiError, api, type ApiArtifact } from "@/lib/api";

const BYTES = new TextEncoder().encode("%PDF-synthetic").buffer;

function artifact(over: Partial<ApiArtifact>): ApiArtifact {
  return {
    id: "a1", kind: "resume_pdf", content_type: "application/pdf", size_bytes: 14, sha256: null,
    resume_id: "r1", resume_version: 2, approval_id: "ap1", template_version: "ats-single-column/2.1",
    created_at: "2026-11-09T10:00:00Z", ...over,
  };
}

describe("export helpers", () => {
  it("picks the current version and prefers the contact-bearing template", () => {
    const list = [
      artifact({ id: "old", resume_version: 1 }),
      artifact({ id: "legacy", template_version: "ats-single-column/legacy-text/1" }),
      artifact({ id: "v2", template_version: "ats-single-column/2.1" }),
      artifact({ id: "docx", kind: "resume_docx" }),
    ];
    expect(pickCurrent(list, 2, "resume_pdf")?.id).toBe("v2");
    expect(pickCurrent(list, 3, "resume_pdf")).toBeNull();
  });

  it("keeps a sane server filename and replaces anything unsafe", () => {
    expect(safeFilename("jobforge-resume-app-v2.pdf", 2, "pdf")).toBe("jobforge-resume-app-v2.pdf");
    expect(safeFilename("../../etc/passwd", 2, "pdf")).toBe("jobforge-resume-v2.pdf");
    expect(safeFilename(null, 3, "docx")).toBe("jobforge-resume-v3.docx");
  });
});

describe("ResumeExportPanel", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    download.mockReset();
    URL.createObjectURL = vi.fn(() => "blob:synthetic");
    URL.revokeObjectURL = vi.fn();
  });

  it("explains that nothing is rendered before approval and export consent", async () => {
    vi.spyOn(api, "listResumeArtifacts").mockResolvedValue([]);
    render(<ResumeExportPanel resumeId="r1" version={2} />);
    expect(await screen.findByText(/No PDF or DOCX exists for version 2/)).toBeInTheDocument();
  });

  it("downloads a file only after its checksum matches", async () => {
    const sha = await sha256Hex(BYTES);
    vi.spyOn(api, "listResumeArtifacts").mockResolvedValue([artifact({ sha256: sha })]);
    download.mockResolvedValue({ bytes: BYTES, filename: "jobforge-resume-app-v2.pdf", contentType: "application/pdf" });
    render(<ResumeExportPanel resumeId="r1" version={2} />);
    fireEvent.click(await screen.findByRole("button", { name: "Download PDF" }));
    expect(await screen.findByRole("status")).toHaveTextContent(/checksum verified/);
    expect(download).toHaveBeenCalledWith("a1");
    expect(URL.createObjectURL).toHaveBeenCalled();
  });

  it("refuses to save bytes that do not match the recorded checksum", async () => {
    vi.spyOn(api, "listResumeArtifacts").mockResolvedValue([artifact({ sha256: "0".repeat(64) })]);
    download.mockResolvedValue({ bytes: BYTES, filename: null, contentType: "application/pdf" });
    render(<ResumeExportPanel resumeId="r1" version={2} />);
    fireEvent.click(await screen.findByRole("button", { name: "Download PDF" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/did not match its recorded checksum/);
    expect(URL.createObjectURL).not.toHaveBeenCalled();
  });

  it("shows the server's refusal, for example withdrawn evidence", async () => {
    vi.spyOn(api, "listResumeArtifacts").mockResolvedValue([artifact({ sha256: "a".repeat(64) })]);
    download.mockRejectedValue(
      new ApiError("Request failed: 409", 409, { detail: "Resume cites inactive evidence; regenerate before export" }),
    );
    render(<ResumeExportPanel resumeId="r1" version={2} />);
    fireEvent.click(await screen.findByRole("button", { name: "Download PDF" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/inactive evidence/);
  });
});
