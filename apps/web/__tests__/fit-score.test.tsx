import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { FitScore, bandFor } from "@/components/FitScore";

describe("bandFor", () => {
  it.each([
    [100, "strong"], [75, "strong"], [74, "moderate"], [60, "moderate"],
    [59, "weak"], [40, "weak"], [39, "no"], [0, "no"],
  ])("maps %i to %s", (score, band) => {
    expect(bandFor(score)).toBe(band);
  });
});

describe("FitScore", () => {
  it("renders the score and its band as text", () => {
    render(<FitScore score={87} />);
    expect(screen.getByText("87")).toBeInTheDocument();
    expect(screen.getByText("Strong")).toBeInTheDocument();
  });

  it("never relies on colour alone", () => {
    render(<FitScore score={45} />);
    // The band is readable as text, not only as a bar colour.
    expect(screen.getByText("Weak")).toBeInTheDocument();
  });

  it("exposes the value to assistive technology", () => {
    render(<FitScore score={62} />);
    const meter = screen.getByRole("meter");
    expect(meter).toHaveAttribute("aria-valuenow", "62");
    expect(meter).toHaveAttribute("aria-valuemax", "100");
  });

  it("clamps out-of-range scores", () => {
    render(<FitScore score={140} />);
    expect(screen.getByRole("meter")).toHaveAttribute("aria-valuenow", "100");
  });
});
