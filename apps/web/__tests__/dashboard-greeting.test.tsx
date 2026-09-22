import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { DashboardGreeting } from "@/components/DashboardGreeting";

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("DashboardGreeting", () => {
  it.each([
    ["11:59", "Good morning"],
    ["12:00", "Good afternoon"],
    ["17:59", "Good afternoon"],
    ["18:00", "Good evening"],
  ])("uses local time at %s", (time, greeting) => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date(`2026-09-17T${time}:00`));
    render(<DashboardGreeting name="Likhith" />);
    expect(screen.getByText(`${greeting}, Likhith.`)).toBeInTheDocument();
  });

  it("keeps a neutral greeting if local time is unavailable", () => {
    vi.spyOn(Date.prototype, "getHours").mockImplementation(() => { throw new Error("unavailable"); });
    render(<DashboardGreeting name="Likhith" />);
    expect(screen.getByText("Welcome back, Likhith.")).toBeInTheDocument();
  });
});
