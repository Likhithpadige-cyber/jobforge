"use client";

import { useCallback, useEffect, useState } from "react";
import { useAuth } from "@/components/AuthGuard";
import { StatusBadge } from "@/components/StatusBadge";
import { ConfirmDialog } from "@/components/review/ConfirmDialog";
import { ReviewErrorState, ReviewLoading, SectionCard } from "@/components/review/ReviewStates";
import { profile, type SubmissionGate } from "@/lib/profile";
import { gateLabel, reviewError, type ReviewError } from "@/lib/review";

/**
 * The persisted submission switch, always shown beside the deployment mode.
 *
 * The switch alone never makes submission live: `SUBMISSION_MODE` must also
 * be `enabled`, and each submission still needs the owner's own confirmed
 * Submit. Every state here is re-read from the server after a change.
 */
export function SubmissionSwitchCard() {
  const { user } = useAuth();
  const isOwner = user?.role === "owner";
  const [gate, setGate] = useState<SubmissionGate | null>(null);
  const [error, setError] = useState<ReviewError | null>(null);
  const [loading, setLoading] = useState(true);
  const [confirming, setConfirming] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setGate(await profile.submissionGate());
      setError(null);
    } catch (cause) {
      setError(reviewError(cause));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  if (loading) return <ReviewLoading label="Loading submission safety settings" />;
  if (error && !gate) return <ReviewErrorState error={error} onRetry={load} backHref="/" />;
  if (!gate) return null;

  const dryRun = gate.submission_mode === "dry_run";
  const turningOn = !gate.persisted_enabled;

  return (
    <SectionCard title="Application submission">
      <div data-testid="settings-gate" className="space-y-4">
        {notice && <p role="status" className="rounded-card border border-signal-strong/25 bg-signal-strong/10 px-4 py-3 text-sm text-ink">{notice}</p>}
        {error && <p role="alert" className="rounded-card border border-signal-review/25 bg-signal-review/10 px-4 py-3 text-sm text-ink">{error.message}</p>}

        <dl className="grid gap-3 sm:grid-cols-2">
          <div className="rounded-card border border-edge p-4">
            <dt className="text-xs font-semibold uppercase tracking-[.12em] text-ink-soft">Deployment mode</dt>
            <dd className="mt-2"><StatusBadge label={dryRun ? "Dry-run · never sends" : "Live mode configured"} tone={dryRun ? "neutral" : "risk"} /></dd>
            <dd className="mt-2 text-xs text-ink-soft">Set by whoever runs this deployment (<code>SUBMISSION_MODE</code>). It cannot be changed here.</dd>
          </div>
          <div className="rounded-card border border-edge p-4">
            <dt className="text-xs font-semibold uppercase tracking-[.12em] text-ink-soft">Submission switch</dt>
            <dd className="mt-2"><StatusBadge label={gate.persisted_enabled ? "On" : "Off"} tone={gate.persisted_enabled ? "risk" : "neutral"} /></dd>
            <dd className="mt-2 text-xs text-ink-soft">Your operational switch, stored in JobForge.</dd>
          </div>
        </dl>

        <p className="text-sm font-medium text-ink" data-testid="gate-summary">{gateLabel(gate)}</p>
        <p className="text-sm text-ink-soft">
          Real email is possible only when the deployment is in live mode <em>and</em> the switch is on. Even then, nothing is sent automatically: each application needs your own confirmed Submit.
        </p>

        {isOwner ? (
          <button type="button" onClick={() => { setNotice(null); setConfirming(true); }} className={`inline-flex min-h-11 items-center rounded-chip px-5 text-sm font-semibold ${turningOn ? "bg-signal-risk text-white hover:opacity-90" : "border border-edge bg-surface text-violet-deep hover:border-violet/40"}`}>
            {turningOn ? "Turn submission switch on…" : "Turn submission switch off…"}
          </button>
        ) : (
          <p data-testid="reviewer-read-only" className="text-sm text-ink-soft">Only the workspace owner can change the submission switch.</p>
        )}
      </div>

      {confirming && (
        <ConfirmDialog
          title={turningOn ? "Turn the submission switch on?" : "Turn the submission switch off?"}
          acknowledgement={
            turningOn
              ? dryRun
                ? "I understand the switch records my intent and sends nothing while the deployment is in dry-run mode."
                : "I understand this allows real submissions, each only after I confirm it."
              : "I understand new submissions will be refused until the switch is turned back on."
          }
          confirmLabel={turningOn ? "Turn on" : "Turn off"}
          busyLabel="Saving…"
          danger={turningOn && !dryRun}
          onCancel={() => setConfirming(false)}
          onConfirm={async () => {
            const result = await profile.setSubmissionEnabled(turningOn);
            setConfirming(false);
            await load();
            setNotice(`The submission switch is ${result.submission_enabled ? "on" : "off"}. The state above was re-read from the server.`);
          }}
        >
          {turningOn ? (
            dryRun ? (
              <p>This deployment is in <strong className="text-ink">dry-run mode</strong>. With the switch on, nothing is sent: real email also needs the deployment&apos;s live mode, which only its operator can enable.</p>
            ) : (
              <>
                <p className="font-medium text-signal-risk">Live mode is configured. With the switch on, a confirmed Submit sends a real email to the employer.</p>
                <p>Nothing is sent automatically. Each application still needs your own confirmed Submit.</p>
              </>
            )
          ) : (
            <p>With the switch off, JobForge refuses new Submit actions, whatever the deployment mode.</p>
          )}
        </ConfirmDialog>
      )}
    </SectionCard>
  );
}
