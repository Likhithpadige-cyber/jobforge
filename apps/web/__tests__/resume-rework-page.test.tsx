import { Suspense } from "react";
import { afterEach, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import Page from "@/app/(workspace)/resume-studio/[applicationId]/page";
import { api, type ApiResume, type ApiResumeReview } from "@/lib/api";

vi.mock("@/components/AppShell", () => ({
  AppShell: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
}));
vi.mock("@/components/ResumeStudioView", () => ({
  ResumeStudioView: ({ resume, onReviewed }: {
    resume: ApiResume; onReviewed: (next: ApiResumeReview) => void;
  }) => <div>
    <p>{resume.summary_text}</p>
    <button onClick={() => onReviewed({ resume_id: "r2" } as ApiResumeReview)}>
      Finish revision
    </button>
  </div>,
}));
afterEach(() => vi.restoreAllMocks());

it("hides the old resume while loading the new version and its claims", async () => {
  let complete!: (resume: ApiResume) => void;
  const latest = vi.spyOn(api, "latestResume")
    .mockResolvedValueOnce({ id: "r1", summary_text: "Old version" } as ApiResume)
    .mockImplementationOnce(() => new Promise((resolve) => { complete = resolve; }));
  const params = Promise.resolve({ applicationId: "a1" });
  await act(async () => {
    render(<Suspense fallback={<div>Starting</div>}><Page params={params} /></Suspense>);
  });
  await screen.findByText("Old version");
  fireEvent.click(screen.getByRole("button", { name: "Finish revision" }));
  await waitFor(() => expect(latest).toHaveBeenCalledTimes(2));
  expect(screen.queryByText("Old version")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Finish revision" })).not.toBeInTheDocument();
  complete({ id: "r2", summary_text: "New verified version" } as ApiResume);
  await screen.findByText("New verified version");
});

it("keeps stale review controls hidden if the new resume cannot be loaded", async () => {
  vi.spyOn(api, "latestResume")
    .mockResolvedValueOnce({ id: "r1", summary_text: "Old version" } as ApiResume)
    .mockRejectedValueOnce(new Error("Fetch failed"));
  const params = Promise.resolve({ applicationId: "a1" });
  await act(async () => {
    render(<Suspense fallback={<div>Starting</div>}><Page params={params} /></Suspense>);
  });
  await screen.findByText("Old version");
  fireEvent.click(screen.getByRole("button", { name: "Finish revision" }));
  await screen.findByText("Could not load this resume");
  expect(screen.queryByText("Old version")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Finish revision" })).not.toBeInTheDocument();
});
