"use client";

import { useCallback, useEffect, useId, useState } from "react";
import { useAuth } from "@/components/AuthGuard";
import { EmptyState } from "@/components/EmptyState";
import { ReviewErrorState, ReviewLoading, SectionCard } from "@/components/review/ReviewStates";
import {
  SECTIONS,
  SECTION_LABEL,
  profile as client,
  profileInput,
  sectionEntry,
  type ProfileDetail,
  type ProfileInput,
  type SectionKind,
} from "@/lib/profile";
import { reviewError, type ReviewError } from "@/lib/review";
import { FactsPanel } from "./FactsPanel";

/**
 * The candidate profile and the facts everything generated must cite.
 *
 * Reviewers see the profile and facts but not contact details or work
 * authorization, and cannot change anything; the server enforces the same.
 */
export function ProfileView() {
  const { user } = useAuth();
  const isOwner = user?.role === "owner";
  const [detail, setDetail] = useState<ProfileDetail | null>(null);
  const [error, setError] = useState<ReviewError | null>(null);
  const [loading, setLoading] = useState(true);
  const [editing, setEditing] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [factsVersion, setFactsVersion] = useState(0);

  const load = useCallback(async () => {
    try {
      setDetail(await client.get());
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

  if (loading) return <ReviewLoading label="Loading profile" />;
  if (error?.kind === "not_found" && !detail) {
    return <EmptyState title="No profile yet" body="Import your master resume to create the profile that every application draws on." />;
  }
  if (error && !detail) return <ReviewErrorState error={error} onRetry={load} backHref="/" />;
  if (!detail) return null;

  const location = [detail.location_city, detail.location_state, detail.location_country].filter(Boolean).join(", ");

  return (
    <div className="space-y-6">
      {notice && <p role="status" className="rounded-card border border-signal-strong/25 bg-signal-strong/10 px-4 py-3 text-sm text-ink">{notice}</p>}

      <header className="rounded-[1.5rem] border border-violet/10 bg-gradient-to-br from-white to-[#F5F1FB] p-6 shadow-raised sm:p-8">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="min-w-0">
            <h2 className="break-words text-2xl font-semibold tracking-tight text-ink sm:text-3xl">{detail.full_name}</h2>
            {detail.headline && <p className="mt-1 text-ink-soft">{detail.headline}</p>}
            {location && <p className="mt-2 text-sm text-ink-soft">{location}{detail.willing_to_relocate ? " · open to relocating" : ""}</p>}
          </div>
          {isOwner && !editing && (
            <button type="button" onClick={() => { setNotice(null); setEditing(true); }} className="inline-flex min-h-11 items-center rounded-chip border border-edge bg-surface px-4 text-sm font-semibold text-violet-deep hover:border-violet/40">Edit basics</button>
          )}
        </div>
        {detail.target_roles.length > 0 && (
          <ul className="mt-4 flex flex-wrap gap-2" aria-label="Target roles">
            {detail.target_roles.map((role) => <li key={role} className="rounded-chip bg-white/85 px-3 py-1 text-sm text-ink">{role}</li>)}
          </ul>
        )}
        {isOwner && (
          <dl className="mt-4 grid gap-2 text-sm sm:grid-cols-3">
            <div><dt className="text-ink-soft">Email</dt><dd className="break-all text-ink">{detail.email ?? "Not set"}</dd></div>
            <div><dt className="text-ink-soft">Phone</dt><dd className="text-ink">{detail.phone ?? "Not set"}</dd></div>
            <div><dt className="text-ink-soft">Work authorization</dt><dd className="text-ink">{detail.work_authorization ? "Recorded (never sent to a model)" : "Not recorded"}</dd></div>
          </dl>
        )}
      </header>

      {editing && (
        <BasicsForm
          detail={detail}
          onCancel={() => setEditing(false)}
          onSaved={async () => {
            setEditing(false);
            await load();
            setNotice("Profile saved. The details above were re-read from the server.");
          }}
        />
      )}

      <div className="grid gap-5 lg:grid-cols-2">
        {SECTIONS.map((kind) => (
          <SectionList
            key={kind}
            kind={kind}
            entries={detail[kind] ?? []}
            canSuggest={isOwner}
            onSuggested={(count) => {
              setNotice(count > 0 ? `${count} draft fact${count === 1 ? "" : "s"} suggested. Verify each one before it can be cited.` : "No new facts could be suggested from that entry.");
              setFactsVersion((value) => value + 1);
            }}
          />
        ))}
      </div>

      <FactsPanel isOwner={isOwner} reloadKey={factsVersion} />
    </div>
  );
}

function SectionList({ kind, entries, canSuggest, onSuggested }: { kind: SectionKind; entries: { id: string; [key: string]: unknown }[]; canSuggest: boolean; onSuggested: (count: number) => void }) {
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function suggest(id: string) {
    if (busy) return;
    setBusy(id);
    setError(null);
    try {
      onSuggested((await client.suggestFacts(kind, id)).length);
    } catch (cause) {
      setError(reviewError(cause).message);
    } finally {
      setBusy(null);
    }
  }

  return (
    <SectionCard title={SECTION_LABEL[kind]}>
      {error && <p role="alert" className="mb-2 text-sm text-signal-risk">{error}</p>}
      {entries.length === 0 ? (
        <p className="text-sm text-ink-soft">Nothing recorded yet.</p>
      ) : (
        <ul className="space-y-2">
          {entries.map((raw) => {
            const entry = sectionEntry(kind, raw);
            return (
              <li key={entry.id} className="flex flex-wrap items-center justify-between gap-2 rounded-chip border border-edge px-3 py-2">
                <div className="min-w-0">
                  <p className="break-words text-sm font-medium text-ink">{entry.title}</p>
                  {entry.detail && <p className="break-words text-xs text-ink-soft">{entry.detail}</p>}
                </div>
                {canSuggest && (
                  <button type="button" onClick={() => void suggest(entry.id)} disabled={busy !== null} className="inline-flex min-h-11 items-center rounded-chip px-3 text-sm font-semibold text-violet-deep hover:underline disabled:opacity-50" aria-label={`Suggest facts from ${entry.title}`}>
                    {busy === entry.id ? "Suggesting…" : "Suggest facts"}
                  </button>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </SectionCard>
  );
}

function BasicsForm({ detail, onCancel, onSaved }: { detail: ProfileDetail; onCancel: () => void; onSaved: () => Promise<void> }) {
  const id = useId();
  const [form, setForm] = useState<ProfileInput>(() => profileInput(detail));
  const [roles, setRoles] = useState(detail.target_roles.join(", "));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const set = <K extends keyof ProfileInput>(key: K, value: ProfileInput[K]) => setForm((current) => ({ ...current, [key]: value }));
  const text = (key: "full_name" | "headline" | "email" | "phone" | "location_city" | "location_state" | "location_country" | "work_authorization", label: string, hint?: string) => (
    <div>
      <label htmlFor={`${id}-${key}`} className="text-sm font-medium text-ink">{label}</label>
      <input id={`${id}-${key}`} value={form[key] ?? ""} onChange={(event) => set(key, key === "full_name" ? event.target.value : event.target.value || null)} required={key === "full_name"} className="mt-1 min-h-11 w-full rounded-chip border border-edge bg-surface px-3 text-sm text-ink" />
      {hint && <p className="mt-1 text-xs text-ink-soft">{hint}</p>}
    </div>
  );

  async function save(event: React.FormEvent) {
    event.preventDefault();
    if (saving) return;
    setSaving(true);
    setError(null);
    try {
      const target_roles = roles.split(",").map((role) => role.trim()).filter(Boolean);
      await client.update({ ...form, target_roles }, detail.version);
      await onSaved();
    } catch (cause) {
      const failure = reviewError(cause);
      setError(failure.kind === "conflict" ? "The profile changed since you opened it. Cancel and edit again to see the latest version." : failure.message);
    } finally {
      setSaving(false);
    }
  }

  return (
    <SectionCard title="Edit basics">
      <form onSubmit={save} className="space-y-4" aria-label="Edit profile basics">
        {error && <p role="alert" className="rounded-card border border-signal-review/25 bg-signal-review/10 px-4 py-3 text-sm text-ink">{error}</p>}
        <div className="grid gap-4 sm:grid-cols-2">
          {text("full_name", "Full name")}
          {text("headline", "Headline")}
          {text("email", "Email")}
          {text("phone", "Phone")}
          {text("location_city", "City")}
          {text("location_state", "State or region")}
          {text("location_country", "Country")}
          {text("work_authorization", "Work authorization", "Entered by you only. Never inferred, never sent to a model, never auto-answered.")}
        </div>
        <div>
          <label htmlFor={`${id}-roles`} className="text-sm font-medium text-ink">Target roles</label>
          <input id={`${id}-roles`} value={roles} onChange={(event) => setRoles(event.target.value)} className="mt-1 min-h-11 w-full rounded-chip border border-edge bg-surface px-3 text-sm text-ink" />
          <p className="mt-1 text-xs text-ink-soft">Separate roles with commas (up to 20).</p>
        </div>
        <label className="flex min-h-11 items-center gap-3 text-sm text-ink">
          <input type="checkbox" checked={form.willing_to_relocate} onChange={(event) => set("willing_to_relocate", event.target.checked)} className="h-5 w-5 accent-violet" />
          Open to relocating
        </label>
        <div className="flex flex-wrap gap-3">
          <button type="submit" disabled={saving || !form.full_name.trim()} className="inline-flex min-h-11 items-center rounded-chip bg-violet px-5 text-sm font-semibold text-white hover:bg-violet-deep disabled:opacity-50">{saving ? "Saving…" : "Save profile"}</button>
          <button type="button" onClick={onCancel} disabled={saving} className="inline-flex min-h-11 items-center rounded-chip border border-edge bg-surface px-5 text-sm font-semibold text-ink hover:border-violet/40">Cancel</button>
        </div>
      </form>
    </SectionCard>
  );
}
