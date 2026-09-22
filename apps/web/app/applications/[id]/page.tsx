"use client";

import Link from "next/link";
import { use, useEffect, useState } from "react";
import { AppShell } from "@/components/AppShell";
import { EmptyState } from "@/components/EmptyState";
import { Skeleton } from "@/components/Skeleton";
import { StatusBadge } from "@/components/StatusBadge";
import { api, type ApiApplication, type ApiJobDetail } from "@/lib/api";
import { applicationStatus, nextAction } from "@/lib/application-view";
import { formatTimestamp, VERIFICATION_LABEL } from "@/lib/resumes";

export default function Page({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const [application, setApplication] = useState<ApiApplication | null>(null);
  const [job, setJob] = useState<ApiJobDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    api.listApplications({ limit: 200 }).then(async list => {
      if (!active) return;
      const found = list.find(item => item.id === id);
      setApplication(found ?? null);
      if (found) {
        try { const detail = await api.getJob(found.job_id); if (active) setJob(detail); }
        catch { /* Application details remain usable without job location. */ }
      }
    }).catch(cause => { if (active) setError(cause instanceof Error ? cause.message : "Could not load this application"); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [id]);

  const status = application ? applicationStatus(application.status) : null;
  const action = application ? nextAction(application.status, application.latest_resume_version !== null) : null;
  const location = job ? [job.location_city, job.location_state, job.location_country].filter(Boolean).join(", ") || (job.is_remote ? "Remote" : null) : null;

  return <AppShell title="Application details" description="The current state of this opportunity.">
    <div id="content" className="mx-auto max-w-[78rem] space-y-6">
      <nav aria-label="Breadcrumb" className="flex flex-wrap items-center gap-2 text-sm text-ink-soft"><Link href="/applications" className="hover:text-violet-deep">Applications</Link><span aria-hidden="true">›</span><span className="text-ink">{application?.job_title ?? "Application details"}</span></nav>
      {loading && <div data-testid="loading" className="space-y-4"><Skeleton className="h-40 w-full" /><Skeleton className="h-52 w-full" /></div>}
      {!loading && error && <EmptyState title="Could not load this application" body={error} />}
      {!loading && !error && !application && <EmptyState title="Application not found" body="This application is not in the 200 most recent records available to this page." />}
      {!loading && application && status && action && <>
        <header className="reveal rounded-[1.5rem] border border-violet/10 bg-gradient-to-br from-white to-[#F5F1FB] p-6 shadow-raised sm:p-8">
          <div className="flex flex-wrap items-start justify-between gap-4"><div><p className="text-xs font-semibold uppercase tracking-[.14em] text-violet-deep">{application.job_company ?? "Company unavailable"}</p><h2 className="mt-2 text-3xl font-semibold tracking-tight text-ink">{application.job_title ?? "Untitled role"}</h2><p className="mt-2 text-sm text-ink-soft">{location ?? "Location unavailable"} · Created {formatTimestamp(application.created_at)}</p></div><StatusBadge label={status.label} tone={status.tone} /></div>
          <div className="mt-7 rounded-card border border-violet/10 bg-white/85 p-5"><p className="text-xs font-semibold uppercase tracking-[.12em] text-violet-deep">Your next step</p><p className="mt-2 text-lg font-semibold text-ink">{application.status === "awaiting_approval" ? "A resume is waiting for your decision" : status.label}</p><p className="mt-1 text-sm text-ink-soft">Open the latest details before taking action.</p><Link href={action.href === "resume" && application.latest_resume_version !== null ? `/resume-studio/${id}` : `/resume-studio`} className="mt-4 inline-flex min-h-11 items-center rounded-chip bg-violet px-5 text-sm font-semibold text-white hover:bg-violet-deep">{action.href === "resume" && application.latest_resume_version !== null ? "Review resume" : "Open resumes"}<span aria-hidden="true" className="ml-2">→</span></Link></div>
        </header>
        <div className="grid gap-5 lg:grid-cols-[1.3fr_.7fr]">
          <section aria-labelledby="timeline-heading" className="reveal rounded-card border border-edge bg-surface p-6 shadow-raised"><h3 id="timeline-heading" className="text-lg font-semibold text-ink">Application timeline</h3><ol className="mt-5 space-y-5 border-l border-violet/20 pl-5"><li><p className="font-medium text-ink">Application created</p><p className="text-sm text-ink-soft">{formatTimestamp(application.created_at)}</p></li><li><p className="font-medium text-ink">Current status: {status.label}</p><p className="text-sm text-ink-soft">Last updated {formatTimestamp(application.updated_at)}</p></li></ol><p className="mt-6 text-xs text-ink-soft">A full event timeline is not available from the current API.</p></section>
          <div className="space-y-5"><section className="reveal rounded-card border border-edge bg-surface p-6 shadow-raised"><h3 className="text-base font-semibold text-ink">Resume used</h3><p className="mt-3 text-sm text-ink">{application.latest_resume_version === null ? "No tailored resume yet" : `Latest tailored resume · version ${application.latest_resume_version}`}</p><p className="mt-1 text-sm text-ink-soft">{application.latest_verification_status ? VERIFICATION_LABEL[application.latest_verification_status] : "Verification unavailable"}</p>{application.latest_resume_version !== null && <Link href={`/resume-studio/${id}`} className="mt-4 inline-flex min-h-11 items-center text-sm font-semibold text-violet-deep hover:underline">Open resume revision →</Link>}</section><section className="reveal rounded-card border border-edge bg-surface p-6 shadow-raised"><h3 className="text-base font-semibold text-ink">Interview and AI activity</h3><p className="mt-3 text-sm leading-6 text-ink-soft">Interview details and an application-specific AI activity feed are not available from the current API.</p></section></div>
        </div>
      </>}
    </div>
  </AppShell>;
}
