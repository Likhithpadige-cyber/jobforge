import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { FitScoreCard } from "@/components/FitScoreCard";
import { ApiError, api, type ApiEvaluation } from "@/lib/api";

const BASE: ApiEvaluation = {
  id: "e1", assessment_status: "assessed", score: 72.4, recommendation: "moderate",
  matched_requirements: { count: 5 }, missing_requirements: { count: 2 },
  gaps: ["Kubernetes", "Terraform"], score_breakdown: {}, profile_version: 3, created_at: "2026-11-10T00:00:00Z",
};

describe("FitScoreCard", () => {
  beforeEach(() => vi.restoreAllMocks());

  it("shows the stored score, recommendation, matches and gaps as deterministic", async () => {
    vi.spyOn(api, "jobEvaluation").mockResolvedValue(BASE);
    render(<FitScoreCard jobId="j1" />);
    expect(await screen.findByTestId("fit-score")).toHaveAccessibleName("Fit score 72 out of 100");
    expect(screen.getByText("Moderate fit")).toBeInTheDocument();
    expect(screen.getByText(/5 requirements met or partly met/)).toBeInTheDocument();
    expect(screen.getByText("Kubernetes")).toBeInTheDocument();
    expect(screen.getByText(/No AI was used/)).toBeInTheDocument();
  });

  it("never invents a score when evidence is insufficient", async () => {
    vi.spyOn(api, "jobEvaluation").mockResolvedValue({
      ...BASE, assessment_status: "insufficient_evidence", score: null, recommendation: null,
      score_breakdown: { insufficient_reasons: ["No verified facts yet"] },
    });
    render(<FitScoreCard jobId="j1" />);
    expect(await screen.findByTestId("fit-status")).toHaveTextContent("Not enough verified evidence");
    expect(screen.getByText("No verified facts yet")).toBeInTheDocument();
    expect(screen.queryByTestId("fit-score")).toBeNull();
  });

  it("distinguishes not evaluated from unavailable", async () => {
    vi.spyOn(api, "jobEvaluation").mockRejectedValue(new ApiError("x", 404));
    const { unmount } = render(<FitScoreCard jobId="j1" />);
    expect(await screen.findByText(/has not been evaluated yet/)).toBeInTheDocument();
    unmount();
    vi.spyOn(api, "jobEvaluation").mockRejectedValue(new ApiError("x", 500));
    render(<FitScoreCard jobId="j1" />);
    expect(await screen.findByRole("alert")).toHaveTextContent("unavailable");
  });
});
