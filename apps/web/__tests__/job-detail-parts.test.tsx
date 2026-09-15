import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { RequirementList } from "@/components/RequirementList";
import { NotWiredNotice } from "@/components/NotWiredNotice";
import { SecurityNotice } from "@/components/SecurityNotice";
import type { ApiRequirement } from "@/lib/api";

const required: ApiRequirement[] = [
  { text: "3+ years of AWS", necessity: "required", kind: "experience",
    years_min: 3, years_max: null, section: "Requirements" },
];
const preferred: ApiRequirement[] = [
  { text: "Kubernetes exposure", necessity: "preferred", kind: "skill",
    years_min: null, years_max: null, section: "Nice to have" },
];

describe("RequirementList", () => {
  it("renders each requirement verbatim", () => {
    render(<RequirementList title="Must have" requirements={required} emptyNote="none" />);
    expect(screen.getByText("3+ years of AWS")).toBeInTheDocument();
  });

  it("shows kind and years when stated", () => {
    render(<RequirementList title="Must have" requirements={required} emptyNote="none" />);
    expect(screen.getByText("Experience")).toBeInTheDocument();
    expect(screen.getByText("3+ years")).toBeInTheDocument();
  });

  it("omits years when not stated", () => {
    render(<RequirementList title="Nice to have" requirements={preferred} emptyNote="none" />);
    expect(screen.queryByText(/years/)).not.toBeInTheDocument();
  });

  it("shows the count", () => {
    render(<RequirementList title="Must have" requirements={required} emptyNote="none" />);
    expect(screen.getByText("1")).toBeInTheDocument();
  });

  it("explains an empty group rather than rendering nothing", () => {
    render(
      <RequirementList title="Nice to have" requirements={[]}
        emptyNote="Nothing was listed as preferred." />,
    );
    expect(screen.getByText("Nothing was listed as preferred.")).toBeInTheDocument();
  });
});

describe("NotWiredNotice", () => {
  it("states the gap and the contract that would close it", () => {
    render(
      <NotWiredNotice title="Fit score not available"
        body="No endpoint returns it." needs="GET /api/v1/jobs/{id}/evaluation" />,
    );
    expect(screen.getByText("Fit score not available")).toBeInTheDocument();
    expect(screen.getByText(/GET \/api\/v1\/jobs/)).toBeInTheDocument();
  });

  it("never claims a value it does not have", () => {
    const { container } = render(
      <NotWiredNotice title="Fit score not available" body="No endpoint returns it." />,
    );
    expect(container.textContent).not.toMatch(/\d{2,3}\s*(—|-)\s*Strong/);
  });
});

describe("SecurityNotice", () => {
  it("explains that the text was read as data, not followed", () => {
    render(<SecurityNotice />);
    expect(
      screen.getByText(/read as content, not followed as instructions/i),
    ).toBeInTheDocument();
  });

  it("reassures that the job is still safe to review", () => {
    render(<SecurityNotice />);
    expect(screen.getByText(/safe to review/i)).toBeInTheDocument();
  });

  it("labels itself as a sample while findings are not stored", () => {
    render(<SecurityNotice isDemo />);
    expect(screen.getByText(/not stored against jobs yet/i)).toBeInTheDocument();
  });
});

describe("provenance honesty", () => {
  it("shows no AI-assisted label anywhere in Job Detail parts", () => {
    const { container } = render(
      <>
        <RequirementList title="Must have" requirements={required} emptyNote="none" />
        <NotWiredNotice title="Fit score not available" body="No endpoint returns it." />
      </>,
    );
    expect(container.textContent).not.toMatch(/AI-assisted/i);
  });
});
