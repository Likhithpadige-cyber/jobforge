"use client";

/**
 * Sign out everywhere (M22 prerequisite): for a lost or shared device.
 * Revokes every session this account holds - this browser included - on
 * the server, then returns to the sign-in page. Signing in again needs the
 * password and the authenticator code. Two-step: nothing happens until the
 * person confirms.
 */

import { useRouter } from "next/navigation";
import { useState } from "react";
import { SectionCard } from "@/components/review/ReviewStates";
import { api, apiErrorText } from "@/lib/api";

export function SignOutEverywhereCard() {
  const router = useRouter();
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  async function confirm() {
    setBusy(true);
    setProblem(null);
    try {
      await api.logoutEverywhere();
      router.replace("/login?signed_out=everywhere");
    } catch (cause) {
      setProblem(apiErrorText(cause, "The sign-out could not be confirmed. Try again; repeating it is safe."));
      setBusy(false);
    }
  }

  const BUTTON = "inline-flex min-h-11 items-center rounded-chip border border-edge px-4 py-2 text-sm font-semibold disabled:opacity-60";

  return (
    <SectionCard title="Sessions and devices">
      <p className="text-sm text-ink-soft">
        Lost a phone, or signed in on a device you no longer control? Sign out everywhere ends every session for this account,
        including this one. You will need your password and authenticator code to sign in again.
      </p>
      {!confirming ? (
        <button type="button" className={`${BUTTON} mt-3 bg-surface text-ink`} onClick={() => setConfirming(true)}>
          Sign out everywhere
        </button>
      ) : (
        <div role="group" aria-label="Confirm signing out everywhere" className="mt-3 flex flex-wrap items-center gap-2">
          <p className="text-sm font-medium text-ink">End every session now, including this one?</p>
          <button type="button" disabled={busy} className={`${BUTTON} bg-inset text-attention`} onClick={() => void confirm()}>
            {busy ? "Signing out…" : "Yes, sign out everywhere"}
          </button>
          <button type="button" disabled={busy} className={`${BUTTON} bg-surface text-ink`} onClick={() => setConfirming(false)}>
            Cancel
          </button>
        </div>
      )}
      {problem && <p role="alert" className="mt-3 text-sm text-danger">{problem}</p>}
    </SectionCard>
  );
}
