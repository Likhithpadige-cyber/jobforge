"use client";

import { useCallback, useEffect, useId, useState } from "react";
import { EmptyState } from "@/components/EmptyState";
import { StatusBadge } from "@/components/StatusBadge";
import { ConfirmDialog } from "@/components/review/ConfirmDialog";
import { ReviewErrorState, ReviewLoading, SectionCard } from "@/components/review/ReviewStates";
import {
  FACT_STATUS_LABEL,
  profile as client,
  type Evidence,
  type EvidenceKind,
  type Fact,
  type FactStatus,
  type FactSummary,
} from "@/lib/profile";
import { formatTimestamp } from "@/lib/resumes";
import { reviewError, type ReviewError } from "@/lib/review";

type Filter = FactStatus | "all";
type Action = "verify" | "unverify" | "retire";

const FILTERS: { value: Filter; label: string }[] = [
  { value: "draft", label: "Awaiting verification" },
  { value: "verified", label: "Verified" },
  { value: "retired", label: "Retired" },
  { value: "all", label: "All" },
];

const ACTION_COPY: Record<Action, { title: string; body: string; acknowledgement: string; confirm: string }> = {
  verify: {
    title: "Verify this fact?",
    body: "Once verified, generated resumes, cover letters and answers may cite it as true about you.",
    acknowledgement: "I confirm this statement is accurate and I can stand behind it.",
    confirm: "Verify",
  },
  unverify: {
    title: "Return this fact to draft?",
    body: "It stops being citable immediately. Anything already citing it will refer to a fact that is no longer verified.",
    acknowledgement: "I understand this fact will no longer be citable until I verify it again.",
    confirm: "Return to draft",
  },
  retire: {
    title: "Retire this fact?",
    body: "Retire a fact that is no longer true. It stops being citable and stays in the record for history.",
    acknowledgement: "I understand a retired fact can no longer be cited.",
    confirm: "Retire",
  },
};

export function FactsPanel({ isOwner, reloadKey = 0 }: { isOwner: boolean; reloadKey?: number }) {
  const [filter, setFilter] = useState<Filter>("draft");
  const [facts, setFacts] = useState<Fact[] | null>(null);
  const [summary, setSummary] = useState<FactSummary | null>(null);
  const [error, setError] = useState<ReviewError | null>(null);
  const [loading, setLoading] = useState(true);
  const [pending, setPending] = useState<{ fact: Fact; action: Action } | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [nextFacts, nextSummary] = await Promise.all([
        client.facts(filter === "all" ? undefined : filter),
        client.factSummary(),
      ]);
      setFacts(nextFacts);
      setSummary(nextSummary);
      setError(null);
    } catch (cause) {
      setError(reviewError(cause));
    } finally {
      setLoading(false);
    }
  }, [filter]);

  useEffect(() => {
    void load();
  }, [load, reloadKey]);

  const run = async (fact: Fact, action: Action) => {
    if (action === "verify") await client.verifyFact(fact.id);
    else if (action === "unverify") await client.unverifyFact(fact.id);
    else await client.retireFact(fact.id);
    setPending(null);
    await load();
    setNotice(`${action === "verify" ? "Verified" : action === "unverify" ? "Returned to draft" : "Retired"}. The list was re-read from the server.`);
  };

  return (
    <SectionCard title="Facts">
      <div className="space-y-4">
        <p className="text-sm text-ink-soft">Everything JobForge writes about you must cite a verified fact. Drafts are never cited until you verify them.</p>
        {summary && (
          <dl className="grid grid-cols-2 gap-3 sm:grid-cols-4" data-testid="fact-summary">
            <Stat label="Verified" value={summary.citable} />
            <Stat label="Awaiting verification" value={summary.draft_awaiting_verification} />
            <Stat label="Retired" value={summary.by_status.retired ?? 0} />
            <Stat label="Never cited" value={summary.never_used} />
          </dl>
        )}

        <div role="group" aria-label="Filter facts" className="flex flex-wrap gap-2">
          {FILTERS.map((item) => (
            <button key={item.value} type="button" aria-pressed={filter === item.value} onClick={() => { setNotice(null); setFilter(item.value); }} className={`inline-flex min-h-11 items-center rounded-chip border px-4 text-sm font-semibold ${filter === item.value ? "border-violet bg-violet text-white" : "border-edge bg-surface text-ink hover:border-violet/40"}`}>
              {item.label}
            </button>
          ))}
        </div>

        {notice && <p role="status" className="rounded-card border border-signal-strong/25 bg-signal-strong/10 px-4 py-3 text-sm text-ink">{notice}</p>}
        {error && facts && <p role="alert" className="rounded-card border border-signal-review/25 bg-signal-review/10 px-4 py-3 text-sm text-ink">The latest refresh failed: {error.message}</p>}

        {loading ? (
          <ReviewLoading label="Loading facts" />
        ) : error && !facts ? (
          <ReviewErrorState error={error} onRetry={load} backHref="/profile" />
        ) : facts && facts.length === 0 ? (
          <EmptyState title="No facts here" body={filter === "draft" ? "Nothing is waiting for verification." : "No facts match this filter."} />
        ) : (
          <ul className="space-y-3">
            {facts?.map((fact) => (
              <FactItem key={fact.id} fact={fact} isOwner={isOwner} onAction={(action) => setPending({ fact, action })} onEdited={async () => { await load(); setNotice("Draft saved."); }} />
            ))}
          </ul>
        )}
      </div>

      {pending && (
        <ConfirmDialog
          title={ACTION_COPY[pending.action].title}
          acknowledgement={ACTION_COPY[pending.action].acknowledgement}
          confirmLabel={ACTION_COPY[pending.action].confirm}
          busyLabel="Saving…"
          danger={pending.action !== "verify"}
          onCancel={() => setPending(null)}
          onConfirm={() => run(pending.fact, pending.action)}
        >
          <p className="rounded-chip bg-muted px-3 py-2 text-ink">“{pending.fact.text}”</p>
          <p>{ACTION_COPY[pending.action].body}</p>
        </ConfirmDialog>
      )}
    </SectionCard>
  );
}

function Stat({ label, value }: { label: string; value: number }) {
  return (
    <div className="rounded-card border border-edge p-3">
      <dt className="text-xs text-ink-soft">{label}</dt>
      <dd className="text-xl font-semibold text-ink">{value}</dd>
    </div>
  );
}

function FactItem({ fact, isOwner, onAction, onEdited }: { fact: Fact; isOwner: boolean; onAction: (action: Action) => void; onEdited: () => Promise<void> }) {
  const id = useId();
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState(fact.text);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showEvidence, setShowEvidence] = useState(false);
  const status = FACT_STATUS_LABEL[fact.status];

  async function save() {
    if (saving) return;
    setSaving(true);
    setError(null);
    try {
      await client.editFact(fact.id, text);
      setEditing(false);
      await onEdited();
    } catch (cause) {
      setError(reviewError(cause).message);
    } finally {
      setSaving(false);
    }
  }

  return (
    <li className="rounded-card border border-edge p-4">
      <div className="flex flex-wrap items-center gap-2">
        <StatusBadge label={status.label} tone={status.tone} />
        <span className="text-xs text-ink-soft">{fact.fact_type} · from {fact.source_table} · cited {fact.usage_count} time{fact.usage_count === 1 ? "" : "s"}</span>
      </div>
      {editing ? (
        <div className="mt-3">
          <label htmlFor={`${id}-text`} className="sr-only">Fact text</label>
          <textarea id={`${id}-text`} value={text} onChange={(event) => setText(event.target.value)} maxLength={2000} rows={3} className="w-full rounded-chip border border-edge bg-surface px-3 py-2 text-sm text-ink" />
          <div className="mt-2 flex flex-wrap gap-2">
            <button type="button" onClick={() => void save()} disabled={saving || !text.trim() || text === fact.text} className="inline-flex min-h-11 items-center rounded-chip bg-violet px-4 text-sm font-semibold text-white disabled:opacity-50">{saving ? "Saving…" : "Save draft"}</button>
            <button type="button" onClick={() => { setEditing(false); setText(fact.text); }} disabled={saving} className="inline-flex min-h-11 items-center rounded-chip border border-edge bg-surface px-4 text-sm font-semibold text-ink">Cancel</button>
          </div>
        </div>
      ) : (
        <p className="mt-2 break-words text-ink">{fact.text}</p>
      )}
      {fact.verified_at && <p className="mt-1 text-xs text-ink-soft">Verified {formatTimestamp(fact.verified_at)}</p>}
      {error && <p role="alert" className="mt-2 text-sm text-signal-risk">{error}</p>}

      <div className="mt-3 flex flex-wrap gap-2">
        {isOwner && fact.status === "draft" && !editing && (
          <>
            <ActionButton onClick={() => onAction("verify")} primary>Verify…</ActionButton>
            <ActionButton onClick={() => setEditing(true)}>Edit</ActionButton>
          </>
        )}
        {isOwner && fact.status === "verified" && <ActionButton onClick={() => onAction("unverify")}>Return to draft…</ActionButton>}
        {isOwner && fact.status !== "retired" && !editing && <ActionButton onClick={() => onAction("retire")}>Retire…</ActionButton>}
        <ActionButton onClick={() => setShowEvidence((value) => !value)} expanded={showEvidence}>{showEvidence ? "Hide evidence" : "Evidence"}</ActionButton>
      </div>
      {showEvidence && <EvidenceList factId={fact.id} canAdd={isOwner} />}
    </li>
  );
}

function ActionButton({ children, onClick, primary = false, expanded }: { children: React.ReactNode; onClick: () => void; primary?: boolean; expanded?: boolean }) {
  return (
    <button type="button" onClick={onClick} aria-expanded={expanded} className={`inline-flex min-h-11 items-center rounded-chip px-4 text-sm font-semibold ${primary ? "bg-violet text-white hover:bg-violet-deep" : "border border-edge bg-surface text-violet-deep hover:border-violet/40"}`}>
      {children}
    </button>
  );
}

function EvidenceList({ factId, canAdd }: { factId: string; canAdd: boolean }) {
  const id = useId();
  const [items, setItems] = useState<Evidence[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [kind, setKind] = useState<EvidenceKind>("attestation");
  const [url, setUrl] = useState("");
  const [note, setNote] = useState("");
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    try {
      setItems(await client.evidence(factId));
      setError(null);
    } catch (cause) {
      setError(reviewError(cause).message);
    }
  }, [factId]);

  useEffect(() => {
    void load();
  }, [load]);

  async function add(event: React.FormEvent) {
    event.preventDefault();
    if (saving) return;
    setSaving(true);
    setError(null);
    try {
      await client.addEvidence(factId, { kind, url, note });
      setUrl("");
      setNote("");
      await load();
    } catch (cause) {
      setError(reviewError(cause).message);
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="mt-3 rounded-card bg-muted p-3">
      {error && <p role="alert" className="mb-2 text-sm text-signal-risk">{error}</p>}
      {items === null && !error ? (
        <p className="text-sm text-ink-soft">Loading evidence…</p>
      ) : items && items.length === 0 ? (
        <p className="text-sm text-ink-soft">No evidence attached.</p>
      ) : (
        <ul className="space-y-2" aria-label="Evidence">
          {items?.map((item) => (
            <li key={item.id} className="text-sm text-ink">
              <span className="font-medium">{item.kind}</span>
              {item.note && <span> · {item.note}</span>}
              {item.url && <span className="break-all text-ink-soft"> · {item.url}</span>}
              <span className="text-xs text-ink-soft"> · added {formatTimestamp(item.created_at)}</span>
            </li>
          ))}
        </ul>
      )}
      {canAdd && (
        <form onSubmit={add} className="mt-3 grid gap-2 sm:grid-cols-[10rem_1fr_1fr_auto]" aria-label="Add evidence">
          <label className="sr-only" htmlFor={`${id}-kind`}>Evidence type</label>
          <select id={`${id}-kind`} value={kind} onChange={(event) => setKind(event.target.value as EvidenceKind)} className="min-h-11 rounded-chip border border-edge bg-surface px-3 text-sm text-ink">
            <option value="attestation">Attestation</option>
            <option value="url">Link</option>
            <option value="document">Document</option>
            <option value="credential">Credential</option>
          </select>
          <label className="sr-only" htmlFor={`${id}-note`}>Note</label>
          <input id={`${id}-note`} value={note} onChange={(event) => setNote(event.target.value)} placeholder="Note" maxLength={2000} className="min-h-11 rounded-chip border border-edge bg-surface px-3 text-sm text-ink" />
          <label className="sr-only" htmlFor={`${id}-url`}>Link</label>
          <input id={`${id}-url`} value={url} onChange={(event) => setUrl(event.target.value)} placeholder="https://…" type="url" className="min-h-11 rounded-chip border border-edge bg-surface px-3 text-sm text-ink" />
          <button type="submit" disabled={saving || (!note.trim() && !url.trim())} className="inline-flex min-h-11 items-center justify-center rounded-chip bg-violet px-4 text-sm font-semibold text-white disabled:opacity-50">{saving ? "Adding…" : "Add"}</button>
        </form>
      )}
    </div>
  );
}
