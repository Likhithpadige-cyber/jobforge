import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { StatusBadge } from "@/components/StatusBadge";
import { ProvenanceTag } from "@/components/ProvenanceTag";

describe("StatusBadge", () => {
  it("always renders the label as text", () => {
    render(<StatusBadge label="Shortlisted" />);
    expect(screen.getByText("Shortlisted")).toBeInTheDocument();
  });

  it("falls back to a neutral tone for an unknown status", () => {
    render(<StatusBadge label="Something new" />);
    expect(screen.getByText("Something new")).toBeInTheDocument();
  });
});

describe("ProvenanceTag", () => {
  it("distinguishes deterministic from AI-assisted", () => {
    const { rerender } = render(<ProvenanceTag kind="deterministic" />);
    expect(screen.getByText("Deterministic")).toBeInTheDocument();
    rerender(<ProvenanceTag kind="ai-assisted" />);
    expect(screen.getByText("AI-assisted")).toBeInTheDocument();
  });

  it("explains what the label means on hover", () => {
    render(<ProvenanceTag kind="deterministic" />);
    // The production copy capitalises the sentence ("Same input always gives
    // the same result."), so match the meaning rather than the casing.
    expect(screen.getByText("Deterministic")).toHaveAttribute(
      "title",
      expect.stringMatching(/same input/i),
    );
  });
});
