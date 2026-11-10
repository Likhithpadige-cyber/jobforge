import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const router = vi.hoisted(() => ({ replace: vi.fn() }));
vi.mock("next/navigation", () => ({ useRouter: () => router }));

import { SignOutEverywhereCard } from "@/components/settings/SignOutEverywhereCard";
import { ApiError, api } from "@/lib/api";

describe("SignOutEverywhereCard", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    router.replace.mockReset();
  });

  it("asks for confirmation before revoking anything", () => {
    const spy = vi.spyOn(api, "logoutEverywhere");
    render(<SignOutEverywhereCard />);
    fireEvent.click(screen.getByRole("button", { name: "Sign out everywhere" }));
    expect(screen.getByRole("group", { name: "Confirm signing out everywhere" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(spy).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Sign out everywhere" })).toBeInTheDocument();
  });

  it("revokes every session and returns to sign-in", async () => {
    const spy = vi.spyOn(api, "logoutEverywhere").mockResolvedValue({ revoked: true });
    render(<SignOutEverywhereCard />);
    fireEvent.click(screen.getByRole("button", { name: "Sign out everywhere" }));
    fireEvent.click(screen.getByRole("button", { name: "Yes, sign out everywhere" }));
    await waitFor(() => expect(router.replace).toHaveBeenCalledWith("/login?signed_out=everywhere"));
    expect(spy).toHaveBeenCalledTimes(1);
  });

  it("a failure shows the reason and stays on the page", async () => {
    vi.spyOn(api, "logoutEverywhere").mockRejectedValue(new ApiError("x", 503, { detail: "session store unavailable" }));
    render(<SignOutEverywhereCard />);
    fireEvent.click(screen.getByRole("button", { name: "Sign out everywhere" }));
    fireEvent.click(screen.getByRole("button", { name: "Yes, sign out everywhere" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("session store unavailable");
    expect(router.replace).not.toHaveBeenCalled();
  });
});
