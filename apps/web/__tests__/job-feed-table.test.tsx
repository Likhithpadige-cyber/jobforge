import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { JobFeedTable } from "@/components/JobFeedTable";
import type { ApiJob } from "@/lib/api";

vi.mock("next/link", () => ({
  default: ({ children, href }: { children: React.ReactNode; href: string }) => (
    <a href={href}>{children}</a>
  ),
}));

function job(overrides: Partial<ApiJob> = {}): ApiJob {
  return {
    id: "j1", source_id: "s1", company_id: null, external_id: null,
    url: "https://example.invalid/j/1", title: "Cloud Engineer",
    normalized_title: "cloud engineer", seniority: "senior",
    location_city: "Dallas", location_state: "TX", location_country: "US",
    is_remote: false, is_hybrid: false, employment_type: "full_time",
    salary_min: null, salary_max: null, salary_currency: null, salary_period: null,
    status: "shortlisted", discard_reason: null, duplicate_of_id: null, priority: 0,
    posted_at: null, first_seen_at: new Date().toISOString(),
    last_seen_at: new Date().toISOString(),
    ...overrides,
  };
}

const noop = () => {};

describe("JobFeedTable", () => {
  it("renders a row per job with a Review match link", () => {
    render(
      <JobFeedTable jobs={[job()]} sourceNames={{ s1: "Demo Source" }}
        loading={false} hasFilters={false} onResetFilters={noop} />,
    );
    expect(screen.getByText("Cloud Engineer")).toBeInTheDocument();
    expect(screen.getByText("Dallas, TX")).toBeInTheDocument();
    const link = screen.getByRole("link", { name: /review match/i });
    expect(link).toHaveAttribute("href", "/job-feed/j1");
  });

  it("resolves the source name", () => {
    render(
      <JobFeedTable jobs={[job()]} sourceNames={{ s1: "Demo Source" }}
        loading={false} hasFilters={false} onResetFilters={noop} />,
    );
    expect(screen.getByText("Demo Source")).toBeInTheDocument();
  });

  it("marks a job that is waiting on the person, in text not only colour", () => {
    render(
      <JobFeedTable jobs={[job({ status: "evaluated" })]} sourceNames={{}}
        loading={false} hasFilters={false} onResetFilters={noop} />,
    );
    expect(screen.getByText("Needs your call")).toBeInTheDocument();
    expect(screen.getByText("Waiting on you")).toBeInTheDocument();
  });

  it("shows a skeleton while loading", () => {
    const { container } = render(
      <JobFeedTable jobs={[]} sourceNames={{}} loading hasFilters={false}
        onResetFilters={noop} />,
    );
    expect(container.querySelectorAll(".animate-pulse").length).toBeGreaterThan(0);
  });

  it("shows the empty state when nothing has been found", () => {
    render(
      <JobFeedTable jobs={[]} sourceNames={{}} loading={false} hasFilters={false}
        onResetFilters={noop} />,
    );
    expect(screen.getByText("No jobs yet")).toBeInTheDocument();
  });

  it("shows a no-results state with a reset action when filtered", async () => {
    const reset = vi.fn();
    render(
      <JobFeedTable jobs={[]} sourceNames={{}} loading={false} hasFilters
        onResetFilters={reset} />,
    );
    expect(screen.getByText("No jobs match these filters")).toBeInTheDocument();
    screen.getByRole("button", { name: /clear filters/i }).click();
    expect(reset).toHaveBeenCalled();
  });

  it("gives the table an accessible caption", () => {
    render(
      <JobFeedTable jobs={[job()]} sourceNames={{}} loading={false}
        hasFilters={false} onResetFilters={noop} />,
    );
    expect(screen.getByRole("table")).toHaveAccessibleName(/jobs found/i);
  });
});
