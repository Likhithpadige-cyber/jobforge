import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { MetricCard } from "@/components/MetricCard";
import { PipelineTrack } from "@/components/PipelineTrack";
import { FitDistribution } from "@/components/FitDistribution";
import { EmptyState } from "@/components/EmptyState";
import { DEMO_FIT_DISTRIBUTION, DEMO_PIPELINE } from "@/lib/demo-data";

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
    render(<PipelineTrack stages={DEMO_PIPELINE} />);
    const items = screen.getAllByRole("listitem");
    expect(items).toHaveLength(DEMO_PIPELINE.length);
    expect(screen.getByText("Discovered")).toBeInTheDocument();
    expect(screen.getByText("Interview")).toBeInTheDocument();
  });

  it("marks the stage that is waiting on the person", () => {
    render(<PipelineTrack stages={DEMO_PIPELINE} />);
    expect(screen.getByText("waiting on you")).toBeInTheDocument();
  });
});

describe("FitDistribution", () => {
  it("prints counts as well as drawing them", () => {
    render(<FitDistribution bands={DEMO_FIT_DISTRIBUTION} />);
    expect(screen.getByText("Strong")).toBeInTheDocument();
    expect(screen.getByText("31")).toBeInTheDocument();
  });

  it("describes the chart for screen readers", () => {
    render(<FitDistribution bands={DEMO_FIT_DISTRIBUTION} />);
    expect(screen.getByRole("img")).toHaveAttribute(
      "aria-label",
      expect.stringContaining("Strong 31"),
    );
  });
});

describe("EmptyState", () => {
  it("says what to do rather than reporting an absence", () => {
    render(<EmptyState title="No jobs yet" body="Import a CSV to get started." />);
    expect(screen.getByText("Import a CSV to get started.")).toBeInTheDocument();
  });
});
