import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { PrepareQuestionsPanel, parseQuestions } from "@/components/review/PrepareQuestionsPanel";
import { ApiError, api } from "@/lib/api";

describe("parseQuestions", () => {
  it("turns non-empty lines into textarea questions", () => {
    expect(parseQuestions(" Why us? \n\nNotice period?\n")).toEqual([
      { question_text: "Why us?", input_type: "textarea" },
      { question_text: "Notice period?", input_type: "textarea" },
    ]);
  });
});

describe("PrepareQuestionsPanel", () => {
  beforeEach(() => vi.restoreAllMocks());

  it("requires a question unless the owner says there are none", async () => {
    const spy = vi.spyOn(api, "prepareApplication");
    render(<PrepareQuestionsPanel applicationId="app1" onPrepared={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Prepare answers" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/at least one question/);
    expect(spy).not.toHaveBeenCalled();
  });

  it("sends the typed questions and refreshes the workspace", async () => {
    const onPrepared = vi.fn();
    const spy = vi.spyOn(api, "prepareApplication").mockResolvedValue({ application_id: "app1", event_id: "e1" });
    render(<PrepareQuestionsPanel applicationId="app1" onPrepared={onPrepared} />);
    fireEvent.change(screen.getByLabelText(/application questions/), { target: { value: "Why this role?\nSalary expectation?" } });
    fireEvent.click(screen.getByRole("button", { name: "Prepare answers" }));
    await waitFor(() => expect(onPrepared).toHaveBeenCalled());
    expect(spy).toHaveBeenCalledWith("app1", { questions: parseQuestions("Why this role?\nSalary expectation?") });
    expect(screen.getByRole("status")).toHaveTextContent(/without any AI call/);
  });

  it("allows an application with no extra questions", async () => {
    const spy = vi.spyOn(api, "prepareApplication").mockResolvedValue({ application_id: "app1", event_id: "e1" });
    render(<PrepareQuestionsPanel applicationId="app1" onPrepared={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "No extra questions" }));
    await waitFor(() => expect(spy).toHaveBeenCalledWith("app1", { questions: [] }));
  });

  it("shows the server's refusal", async () => {
    vi.spyOn(api, "prepareApplication").mockRejectedValue(
      new ApiError("Request failed: 409", 409, { detail: "application is not resume_ready" }),
    );
    render(<PrepareQuestionsPanel applicationId="app1" onPrepared={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "No extra questions" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/not resume_ready/);
  });
});
