"use client";

/**
 * Owner-only: request a first tailored resume draft for one job.
 *
 * Drafting is model work. This deployment's mode decides what that means,
 * and the panel says so before anything is requested:
 *   - replay: live AI is off. A draft exists only where a recorded model
 *     response matches this exact job and profile (the fictional demo). A
 *     new job is queued and then refused by the worker - nothing is invented.
 *   - record/live: a model provider is called and requests may be billable.
 * Prerequisites come from the API (latest master resume, the job's
 * deterministic evaluation); the server re-checks all of them.
 */

import Link from "next/link";
import { useEffect, useState } from "react";
import { useAuth } from "@/components/AuthGuard";
import { ApiError, api, apiErrorText, type RuntimeModes } from "@/lib/api";
import { profile as profileApi } from "@/lib/profile";

interface Prerequisites {
  modes: RuntimeModes | null;
  profileId: string | null;
  masterId: string | null;
  evaluationId: string | null;
}

async function orNull<T>(promise: Promise<T>): Promise<T | null> {
  try {
    return await promise;
  } catch (cause) {
    if (cause instanceof ApiError && cause.status === 404) return null;
    throw cause;
  }
}

export function FirstDraftPanel({ jobId }: { jobId: string }) {
  const { user } = useAuth();
  const [state, setState] = useState<Prerequisites | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<{ applicationId: string } | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const isOwner = user?.role === "owner";

  useEffect(() => {
    if (!isOwner) return;
    let cancelled = false;
    (async () => {
      try {
        const [modes, profile, evaluation] = await Promise.all([
          api.runtimeModes(),
          orNull(profileApi.get()),
          orNull(api.jobEvaluation(jobId)),
        ]);
        const master = profile ? await orNull(profileApi.latestMasterResume(profile.id)) : null;
        if (!cancelled)
          setState({
            modes,
            profileId: profile?.id ?? null,
            masterId: master?.resume_id ?? null,
            evaluationId: evaluation?.id ?? null,
          });
      } catch (cause) {
        if (!cancelled) setLoadError(apiErrorText(cause, "Draft prerequisites could not be checked."));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [isOwner, jobId]);

  if (!isOwner) {
    return (
      <p className="rounded-chip bg-inset px-3 py-2 text-xs text-ink-soft">
        Only the workspace owner can request a resume draft.
      </p>
    );
  }

  async function request() {
    if (!state?.profileId || !state.masterId || !state.evaluationId) return;
    setBusy(true);
    setProblem(null);
    try {
      const queued = await api.requestFirstDraft(jobId, {
        profile_id: state.profileId,
        master_resume_id: state.masterId,
        evaluation_id: state.evaluationId,
      });
      setResult({ applicationId: queued.application_id });
    } catch (cause) {
      setProblem(apiErrorText(cause, "The draft could not be requested."));
    } finally {
      setBusy(false);
    }
  }

  const missing: string[] = [];
  if (state && !state.profileId) missing.push("Create your profile first.");
  if (state && state.profileId && !state.masterId) missing.push("Import a master resume on Profile & Facts first.");
  if (state && !state.evaluationId) missing.push("This job has not been evaluated yet; the evaluator scores new jobs in the background.");
  const replay = state?.modes?.llm_mode === "replay";

  return (
    <section aria-labelledby="first-draft-heading" className="rounded-card border border-edge bg-surface p-5">
      <h2 id="first-draft-heading" className="text-sm font-semibold text-ink">
        Tailored resume draft
      </h2>
      {loadError && <p role="alert" className="mt-2 text-xs text-danger">{loadError}</p>}
      {!state && !loadError && <p className="mt-2 text-xs text-ink-soft">Checking what is available…</p>}
      {state?.modes && (
        <p data-testid="draft-mode" className="mt-2 rounded-chip bg-inset px-3 py-2 text-xs leading-5 text-ink-soft">
          {replay ? (
            <>
              <span className="font-semibold text-ink">Replay demonstration only.</span> Live AI is disabled on this
              deployment. A draft is produced only where a recorded model response exists for this exact job and
              profile, as in the fictional demo. For a newly imported job the request is refused by the drafting
              worker; nothing is invented.
            </>
          ) : (
            <>
              <span className="font-semibold text-ink">Live AI is enabled on this deployment.</span> Requesting a
              draft calls a model provider and may be billable.
            </>
          )}
        </p>
      )}
      {missing.length > 0 && (
        <ul className="mt-3 list-disc space-y-1 pl-5 text-xs text-ink-soft">
          {missing.map((m) => (
            <li key={m}>{m}</li>
          ))}
        </ul>
      )}
      {state && missing.length === 0 && !result && (
        <button
          type="button"
          disabled={busy}
          onClick={() => void request()}
          className="mt-3 inline-flex min-h-11 w-full items-center justify-center rounded-chip border border-edge bg-inset px-3 py-2 text-sm font-semibold text-attention disabled:opacity-60"
        >
          {busy ? "Requesting…" : replay ? "Request replay draft" : "Request AI draft"}
        </button>
      )}
      {result && (
        <p role="status" className="mt-3 text-xs text-success">
          Draft requested. The drafting worker runs in the background;{" "}
          <Link className="text-accent underline" href={`/applications/${result.applicationId}`}>
            open the application
          </Link>{" "}
          to follow its status.
        </p>
      )}
      {problem && <p role="alert" className="mt-3 text-xs text-danger">{problem}</p>}
    </section>
  );
}
