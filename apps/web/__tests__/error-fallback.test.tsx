import { fireEvent, render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { ErrorFallback } from "@/components/ErrorFallback";

it("offers accessible error recovery without exposing exception details", () => {
  const reset = vi.fn();
  render(<ErrorFallback reset={reset} />);
  expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("This page could not be loaded");
  expect(screen.getByRole("alert")).toHaveTextContent("Try loading it again");
  fireEvent.click(screen.getByRole("button", { name: "Try again" }));
  expect(reset).toHaveBeenCalledTimes(1);
});
