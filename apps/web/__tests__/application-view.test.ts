import { describe, expect, it } from "vitest";
import type { ApiApplication } from "@/lib/api";
import { applicationGroup, applicationStatus, filterApplications } from "@/lib/application-view";

const item = (id: string, status: ApiApplication["status"], company: string, updated: string): ApiApplication => ({
  id, job_id: id, status, job_title: "Cloud Engineer", job_company: company,
  created_at: updated, updated_at: updated, resume_count: 0,
  latest_resume_version: null, latest_verification_status: null,
});

describe("application workspace", () => {
  const rows = [
    item("a", "awaiting_approval", "Acme", "2026-09-16T10:00:00Z"),
    item("b", "submitted", "Beacon", "2026-09-17T10:00:00Z"),
    item("c", "created", "Cedar", "2026-09-15T10:00:00Z"),
  ];

  it("uses backend states for attention and application labels", () => {
    expect(applicationGroup("awaiting_approval")).toBe("Needs attention");
    expect(applicationStatus("submitted").label).toBe("Applied");
    expect(applicationStatus("created").label).toBe("Saved");
  });

  it("filters the loaded records and sorts by update time", () => {
    expect(filterApplications(rows, "", "All", "recent").map(row => row.id)).toEqual(["b", "a", "c"]);
    expect(filterApplications(rows, "acme", "Needs attention", "recent").map(row => row.id)).toEqual(["a"]);
    expect(filterApplications(rows, "", "All", "company").map(row => row.id)).toEqual(["a", "b", "c"]);
  });
});
