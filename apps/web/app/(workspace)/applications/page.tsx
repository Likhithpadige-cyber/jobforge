"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { AppShell } from "@/components/AppShell";
import { EmptyState } from "@/components/EmptyState";
import { SkeletonRow } from "@/components/Skeleton";
import { StatusBadge } from "@/components/StatusBadge";
import { api, type ApiApplication } from "@/lib/api";
import { APPLICATION_GROUPS, applicationGroup, applicationStatus, filterApplications, nextAction, type ApplicationGroup } from "@/lib/application-view";
import { formatTimestamp, VERIFICATION_LABEL } from "@/lib/resumes";
import { APPLIED_VIA_LABEL, TRACKING_FILTERS, trackingLabel, type TrackingFilter } from "@/lib/tracking";

export default function Page() {
  const [items, setItems] = useState<ApiApplication[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [group, setGroup] = useState<ApplicationGroup>("All");
  const [sort, setSort] = useState<"recent" | "oldest" | "company">("recent");
  const [outcome, setOutcome] = useState<TrackingFilter>("any");

  // The outcome filter runs on the server, so it covers every application,
  // not just the 200 loaded here.
  useEffect(() => {
    let active = true;
    setLoading(true);
    setError(null);
    api.listApplications({ limit: 200, ...TRACKING_FILTERS[outcome].query }).then(result => { if (active) setItems(result); })
      .catch(cause => { if (active) setError(cause instanceof Error ? cause.message : "Could not load applications"); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [outcome]);

  const visible = useMemo(() => filterApplications(items, query, group, sort), [items, query, group, sort]);
  const attention = items.filter(item => applicationGroup(item.status) === "Needs attention").length;
  // Sent by JobForge or recorded as sent by the owner (manual handoff).
  const applied = items.filter(item => item.applied_via !== null).length;

  return <AppShell title="Applications" description="Every opportunity, its latest step, and what needs you next.">
    <div id="content" className="mx-auto max-w-[84rem] space-y-7">
      <section className="reveal rounded-[1.5rem] border border-edge bg-surface p-6 shadow-raised sm:p-8">
        <p className="text-xs font-semibold uppercase tracking-[.16em] text-accent">Your workspace</p>
        <div className="mt-3 flex flex-wrap items-end justify-between gap-5">
          <div><h2 className="text-3xl font-semibold tracking-tight text-ink">Applications in motion</h2><p className="mt-2 max-w-2xl text-sm leading-6 text-ink-soft">Find an application, see its latest status, and open the next step. These records come from your JobForge account.</p></div>
          <div className="flex gap-3 text-center"><div className="rounded-card border border-edge bg-surface px-5 py-3"><strong className="block text-2xl text-ink">{items.length}</strong><span className="text-xs text-ink-soft">Tracked</span></div><div className="rounded-card border border-edge bg-surface px-5 py-3"><strong className="block text-2xl text-attention">{attention}</strong><span className="text-xs text-ink-soft">Need attention</span></div><div className="rounded-card border border-edge bg-surface px-5 py-3"><strong className="block text-2xl text-success">{applied}</strong><span className="text-xs text-ink-soft">Applied</span></div></div>
        </div>
      </section>

      <section aria-label="Find applications" className="space-y-4">
        <div className="flex flex-col gap-3 sm:flex-row">
          <label className="min-w-0 flex-1 text-sm font-medium text-ink">Search applications<input type="search" value={query} onChange={event => setQuery(event.target.value)} placeholder="Company, role, or status" className="mt-1.5 h-11 w-full rounded-chip border border-edge bg-surface px-4 text-sm font-normal text-ink placeholder:text-ink-soft" /></label>
          <label className="text-sm font-medium text-ink">Outcome<select value={outcome} onChange={event => setOutcome(event.target.value as TrackingFilter)} className="mt-1.5 h-11 w-full rounded-chip border border-edge bg-surface px-4 text-sm font-normal text-ink sm:w-48">{(Object.keys(TRACKING_FILTERS) as TrackingFilter[]).map(key => <option key={key} value={key}>{TRACKING_FILTERS[key].label}</option>)}</select></label>
          <label className="text-sm font-medium text-ink">Sort by<select value={sort} onChange={event => setSort(event.target.value as typeof sort)} className="mt-1.5 h-11 w-full rounded-chip border border-edge bg-surface px-4 text-sm font-normal text-ink sm:w-44"><option value="recent">Recent activity</option><option value="oldest">Oldest activity</option><option value="company">Company A–Z</option></select></label>
        </div>
        <div role="group" aria-label="Filter applications by status" className="flex flex-wrap gap-2">{APPLICATION_GROUPS.map(value => <button key={value} type="button" aria-pressed={group === value} onClick={() => setGroup(value)} className={`min-h-11 rounded-full border px-4 text-sm font-medium transition-transform ${group === value ? "border-edge bg-action text-action-ink" : "border-edge bg-surface text-ink-soft hover:border-control hover:text-accent"}`}>{value}</button>)}</div>
        <p className="text-xs text-ink-soft">Outcomes are your own record, set on each application&apos;s Tracking card. “Applied” counts applications sent by JobForge or recorded as sent by you.</p>
      </section>

      {loading && <div className="rounded-card border border-edge bg-surface" data-testid="loading"><SkeletonRow /><SkeletonRow /><SkeletonRow /></div>}
      {!loading && error && <EmptyState title="Could not load applications" body={`${error}. Check the API connection and reload this page.`} />}
      {!loading && !error && visible.length === 0 && <EmptyState title={items.length ? "No matching applications" : "No applications yet"} body={items.length ? "Try another search or status filter." : "When an application is created, it will appear here with its real status and resume progress."} />}
      {!loading && !error && visible.length > 0 && <div className="stagger grid gap-4 xl:grid-cols-2">{visible.map(item => {
        const status = applicationStatus(item.status);
        const action = nextAction(item.status, item.latest_resume_version !== null);
        return <article key={item.id} className="reveal lift rounded-[1.25rem] border border-edge bg-surface p-5 shadow-raised sm:p-6">
          <div className="flex flex-wrap items-start justify-between gap-3"><div className="min-w-0"><p className="text-xs font-semibold uppercase tracking-[.12em] text-accent">{item.job_company ?? "Company unavailable"}</p><h3 className="mt-1 text-lg font-semibold text-ink">{item.job_title ?? "Untitled role"}</h3><p className="mt-1 text-xs text-ink-soft">Location not available in application record</p></div><StatusBadge label={status.label} tone={status.tone} /></div>
          <dl className="mt-5 grid gap-3 border-t border-edge pt-4 text-sm sm:grid-cols-3"><div><dt className="text-xs text-ink-soft">Last activity</dt><dd className="mt-1 font-medium text-ink">{formatTimestamp(item.updated_at)}</dd></div><div><dt className="text-xs text-ink-soft">Resume</dt><dd className="mt-1 font-medium text-ink">{item.latest_resume_version === null ? "Not generated" : `Version ${item.latest_resume_version}`}</dd></div><div><dt className="text-xs text-ink-soft">Verification</dt><dd className="mt-1 font-medium text-ink">{item.latest_verification_status ? VERIFICATION_LABEL[item.latest_verification_status] : "Not available"}</dd></div></dl>
          <div className="mt-5 flex flex-wrap items-center justify-between gap-3"><p className="text-xs text-ink-soft" data-testid="tracking-line"><span className="font-medium text-ink">{trackingLabel(item)}</span>{item.applied_via ? ` · ${APPLIED_VIA_LABEL[item.applied_via]} on ${item.applied_on}` : ""}{item.follow_up_on ? ` · follow up ${item.follow_up_on}` : ""}</p><Link href={action.href === "resume" && item.latest_resume_version !== null ? `/resume-studio/${item.id}` : `/applications/${item.id}`} className="inline-flex min-h-11 items-center rounded-chip bg-action px-4 text-sm font-semibold text-action-ink hover:bg-action-hover">{action.label} <span aria-hidden="true" className="ml-2">→</span></Link></div>
        </article>;
      })}</div>}
      {items.length === 200 && <p className="text-xs text-ink-soft">Showing the 200 most recent applications. Search and filters apply to this loaded set.</p>}
    </div>
  </AppShell>;
}
