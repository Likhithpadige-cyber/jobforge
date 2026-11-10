"use client";

/**
 * Owner-only: hand the employer's application questions to JobForge.
 *
 * POST /applications/{id}/prepare (resume_ready -> questions_pending). The
 * application worker then classifies the questions, applies preference gates
 * and grounds answers in verified facts deterministically - no AI call.
 * Questions are typed in by the owner; JobForge never reads a live ATS form.
 */

import { useState } from "react";
import { api, apiErrorText } from "@/lib/api";

export const MAX_QUESTIONS = 200;

/** One non-empty line per question; long free-text answers by default. */
export function parseQuestions(text: string) {
  return text
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean)
    .map((question_text) => ({ question_text: question_text.slice(0, 2000), input_type: "textarea" as const }));
}

export function PrepareQuestionsPanel({ applicationId, onPrepared }: { applicationId: string; onPrepared: () => void }) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ tone: "ok" | "error"; text: string } | null>(null);

  async function send(noQuestions: boolean) {
    const questions = noQuestions ? [] : parseQuestions(text);
    if (!noQuestions && questions.length === 0) {
      setMessage({ tone: "error", text: "Enter at least one question, one per line, or choose “No extra questions”." });
      return;
    }
    if (questions.length > MAX_QUESTIONS) {
      setMessage({ tone: "error", text: `At most ${MAX_QUESTIONS} questions can be prepared at once.` });
      return;
    }
    setBusy(true);
    setMessage(null);
    try {
      await api.prepareApplication(applicationId, { questions });
      setMessage({
        tone: "ok",
        text: "Queued. The application worker classifies these questions and drafts grounded answers without any AI call; refresh to see them.",
      });
      onPrepared();
    } catch (cause) {
      setMessage({ tone: "error", text: apiErrorText(cause, "The questions could not be prepared.") });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mt-3 space-y-3">
      <label className="block text-sm text-ink">
        The employer&apos;s application questions, one per line
        <textarea
          className="mt-1 min-h-28 w-full rounded-chip border border-edge bg-surface px-3 py-2 text-sm text-ink"
          value={text}
          onChange={(event) => setText(event.target.value)}
        />
      </label>
      <div className="flex flex-wrap gap-2">
        <button
          type="button"
          disabled={busy}
          onClick={() => void send(false)}
          className="inline-flex min-h-11 items-center rounded-chip border border-edge bg-inset px-4 py-2 text-sm font-semibold text-attention disabled:opacity-60"
        >
          {busy ? "Preparing…" : "Prepare answers"}
        </button>
        <button
          type="button"
          disabled={busy}
          onClick={() => void send(true)}
          className="inline-flex min-h-11 items-center rounded-chip border border-edge bg-surface px-4 py-2 text-sm font-semibold text-ink disabled:opacity-60"
        >
          No extra questions
        </button>
      </div>
      {message && (
        <p role={message.tone === "error" ? "alert" : "status"} className={`text-sm ${message.tone === "error" ? "text-danger" : "text-success"}`}>
          {message.text}
        </p>
      )}
    </div>
  );
}
