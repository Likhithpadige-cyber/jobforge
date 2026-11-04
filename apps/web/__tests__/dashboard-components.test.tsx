import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { MetricCard } from "@/components/MetricCard";
import { PipelineTrack } from "@/components/PipelineTrack";
import { EmptyState } from "@/components/EmptyState";
import type { PipelineStage } from "@/lib/dashboard-priority";

const STAGES: PipelineStage[] = [
  { key: "jobs", label: "Jobs found", count: 12 },
  { key: "Preparing", label: "Preparing", count: 1 },
  { key: "Needs attention", label: "Needs attention", count: 2, note: "waiting on you" },
  { key: "Submitted", label: "Applied", count: 3 },
];

describe("MetricCard", () => {
  it("renders value and label", () => {
    render(<MetricCard label="Strong matches" value={31} hint="75 or above" />);
    expect(screen.getByText("31")).toBeInTheDocument();
    expect(screen.getByText("Strong matches")).toBeInTheDocument();
  });

  it("omits the hint when there is nothing to say", () => {
    render(<MetricCard label="Sent" value={0} />);
    expect(screen.getByText("0")).toBeInTheDocument();
  });
});

describe("PipelineTrack", () => {
  it("renders every stage in order", () => {
    render(<PipelineTrack stages={STAGES} />);
    const items = screen.getAllByRole("listitem");
    expect(items).toHaveLength(STAGES.length);
    expect(screen.getByText("Jobs found")).toBeInTheDocument();
    expect(screen.getByText("Applied")).toBeInTheDocument();
  });

  it("marks the stage that is waiting on the person", () => {
    render(<PipelineTrack stages={STAGES} />);
    expect(screen.getByText("waiting on you")).toBeInTheDocument();
  });
});

describe("EmptyState", () => {
  it("says what to do rather than reporting an absence", () => {
    render(<EmptyState title="No jobs yet" body="Import a CSV to get started." />);
    expect(screen.getByText("Import a CSV to get started.")).toBeInTheDocument();
  });
});
