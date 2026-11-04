"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { useAuth } from "@/components/AuthGuard";
import { DashboardGreeting } from "@/components/DashboardGreeting";
import { Icon } from "@/components/Icon";
import { PipelineTrack } from "@/components/PipelineTrack";
import { SectionHeader } from "@/components/SectionHeader";
import { StatusBadge } from "@/components/StatusBadge";
import { ReviewLoading } from "@/components/review/ReviewStates";
import { AGENT_LABEL, PIPELINE_LABEL, RUN_STATUS, activity, formatCost, type AgentRun, type AgentRunSummary } from "@/lib/activity";
import { api, type ApiApplication } from "@/lib/api";
import { getDashboardPriority, pipelineStages, submissionProblems } from "@/lib/dashboard-priority";
import { profile, type FactSummary } from "@/lib/profile";
import { formatTimestamp } from "@/lib/resumes";
import { APPROVAL_KIND_LABEL, approvalHref, review, type ApprovalInboxItem } from "@/lib/review";

interface Data {
  approvals: ApprovalInboxItem[] | null;
  applications: ApiApplication[] | null;
  jobsFound: number | null;
  runs: AgentRun[] | null;
  runSummary: AgentRunSummary | null;
  facts: FactSummary | null;
  firstName: string | null;
}

async function maybe<T>(call: Promise<T>): Promise<T | null> {
  try {
    return await call;
  } catch {
    return null;
  }
}

/**
 * The real state of the workspace, from the API only.
 *
 * Each source loads independently: one that fails shows "Unavailable"
 * rather than a zero, so a count is never invented.
 */
export function Dashboard() {
  const { user } = useAuth();
  const isOwner = user?.role === "owner";
  const [data, setData] = useState<Data | null>(null);

  const load = useCallback(async () => {
    const [approvals, applications, jobCounts, runs, runSummary, facts, owner] = await Promise.all([
      maybe(review.approvals({ status: "pending", limit: 50 })),
      maybe(api.listApplications({ limit: 200 })),
      maybe(api.jobCounts()),
      maybe(activity.runs({ limit: 5 })),
      maybe(activity.summary(7)),
      maybe(profile.factSummary()),
      // Only the owner is greeted by the candidate's name.
      isOwner ? maybe(profile.get()) : Promise.resolve(null),
    ]);
    setData({
      approvals,
      applications,
      jobsFound: jobCounts?.total ?? null,
      runs,
      runSummary,
      facts,
      firstName: owner?.full_name.trim().split(/\s+/)[0] || null,
    });
  }, [isOwner]);

  useEffect(() => {
    void load();
  }, [load]);

  if (!data) return <ReviewLoading label="Loading dashboard" />;

  const applications = data.applications ?? [];
  const submitted = applications.filter((item) => item.status === "submitted").length;
  const ready = applications.filter((item) => item.status === "submission_ready").length;
  const priority = getDashboardPriority({
    isOwner,
    pendingApprovals: data.approvals?.length ?? null,
    submissionProblems: submissionProblems(applications),
    readyToSubmit: ready,
    draftFacts: data.facts?.draft_awaiting_verification ?? null,
    jobsFound: data.jobsFound,
    applications: applications.length,
  });
  const unavailable = [data.approvals, data.applications, data.jobsFound, data.runs].some((value) => value === null);

  return (
    <div className="space-y-10">
      {unavailable && (
        <div role="alert" className="flex flex-wrap items-center justify-between gap-3 rounded-card border border-signal-review/25 bg-signal-review/10 px-4 py-3 text-sm text-ink">
          <span>Some of this dashboard could not be loaded. Counts marked “—” are unknown, not zero.</span>
          <button type="button" onClick={() => { setData(null); void load(); }} className="inline-flex min-h-11 items-center rounded-chip border border-edge bg-surface px-4 font-semibold text-violet-deep hover:border-violet/40">Retry</button>
        </div>
      )}

      <section className="reveal relative overflow-hidden rounded-[1.5rem] border border-violet/10 bg-gradient-to-br from-white via-[#FBFAFF] to-[#F3F0FA] px-6 py-8 shadow-raised sm:px-9 sm:py-10" aria-labelledby="welcome-heading">
        <div className="relative grid items-center gap-8 lg:grid-cols-[1.35fr_.65fr]">
          <div>
            <p className="mb-3 text-xs font-semibold uppercase tracking-[0.16em] text-violet-deep">Your career, in motion</p>
            <h2 id="welcome-heading" className="max-w-2xl text-3xl font-semibold leading-tight tracking-[-.035em] text-ink sm:text-4xl"><DashboardGreeting name={data.firstName} /></h2>
            <p className="mt-4 max-w-xl text-sm leading-7 text-ink-soft">Everything here is read from your workspace. Every decision stays with a person.</p>
          </div>
          <div className="rounded-card border border-violet/10 bg-white/90 p-5 shadow-raised" data-testid="priority">
            <div className="mb-4 flex items-center gap-2 text-xs font-semibold text-violet-deep"><span className="flex h-7 w-7 items-center justify-center rounded-full bg-violet-soft"><Icon name={priority.icon} /></span>{priority.eyebrow}</div>
            <h3 className="text-lg font-semibold leading-snug text-ink">{priority.title}</h3>
            <p className="mt-2 text-sm leading-6 text-ink-soft">{priority.body}</p>
            <Link href={priority.href} className="mt-5 inline-flex min-h-11 items-center gap-2 rounded-chip bg-violet px-4 text-sm font-semibold text-white hover:bg-violet-deep">{priority.action}<Icon name="arrow" /></Link>
          </div>
        </div>
      </section>

      <section aria-label="At a glance" className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4" data-testid="metrics">
        <Metric label="Jobs found" value={data.jobsFound} icon="feed" href="/job-feed" />
        <Metric label="Applications" value={data.applications ? applications.length : null} icon="applications" href="/applications" />
        <Metric label="Waiting on you" value={data.approvals ? data.approvals.length : null} icon="approvals" href="/approvals" hint="Nothing is sent until a person approves" />
        <Metric label="Applied" value={data.applications ? submitted : null} icon="spark" href="/applications" />
      </section>

      {data.applications && (
        <section aria-labelledby="pipeline-heading">
          <SectionHeader id="pipeline-heading" title="Your applications" description="Where every application stands right now." />
          <PipelineTrack stages={pipelineStages(applications, data.jobsFound)} />
        </section>
      )}

      <div className="grid gap-9 lg:grid-cols-2">
        <section aria-labelledby="decisions-heading">
          <SectionHeader id="decisions-heading" title="Needs a decision" description="The important calls stay with you." action={<Link href="/approvals" className="text-sm font-semibold text-violet-deep hover:underline">All approvals</Link>} />
          <div className="rounded-card border border-edge bg-surface p-5 shadow-raised">
            {data.approvals === null ? (
              <p className="text-sm text-signal-risk">Approvals are unavailable right now.</p>
            ) : data.approvals.length === 0 ? (
              <p className="text-sm text-ink-soft">Nothing is waiting for a decision.</p>
            ) : (
              <ul className="space-y-2">
                {data.approvals.slice(0, 4).map((item) => (
                  <li key={item.approval_id}>
                    <Link href={approvalHref(item)} className="flex min-h-11 flex-wrap items-center justify-between gap-2 rounded-chip border border-edge px-3 py-2 text-sm hover:border-violet/40">
                      <span className="min-w-0"><span className="font-medium text-ink">{APPROVAL_KIND_LABEL[item.kind]}</span> <span className="text-ink-soft">· {item.job_title ?? "Untitled role"}{item.job_company ? ` at ${item.job_company}` : ""}</span></span>
                      <span className="text-xs text-ink-soft">since {formatTimestamp(item.requested_at)} →</span>
                    </Link>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </section>

        <section aria-labelledby="agents-heading">
          <SectionHeader id="agents-heading" title="Recent agent activity" description={data.runSummary ? `${data.runSummary.runs} runs in 7 days · ${formatCost(data.runSummary.cost_usd)} model cost` : "What the agents did recently."} action={<Link href="/agent-activity" className="text-sm font-semibold text-violet-deep hover:underline">All activity</Link>} />
          <div className="rounded-card border border-edge bg-surface p-5 shadow-raised">
            {data.runs === null ? (
              <p className="text-sm text-signal-risk">Agent activity is unavailable right now.</p>
            ) : data.runs.length === 0 ? (
              <p className="text-sm text-ink-soft">No agent has run yet.</p>
            ) : (
              <ul className="divide-y divide-edge">
                {data.runs.map((run) => (
                  <li key={run.id} className="flex flex-wrap items-center justify-between gap-2 py-2 text-sm">
                    <span className="min-w-0 text-ink">{PIPELINE_LABEL[run.pipeline]} <span className="text-ink-soft">· {AGENT_LABEL[run.agent]} · {formatTimestamp(run.started_at)}</span></span>
                    <StatusBadge label={RUN_STATUS[run.status].label} tone={RUN_STATUS[run.status].tone} />
                  </li>
                ))}
              </ul>
            )}
          </div>
        </section>
      </div>
    </div>
  );
}

function Metric({ label, value, icon, href, hint }: { label: string; value: number | null; icon: "feed" | "applications" | "approvals" | "spark"; href: string; hint?: string }) {
  return (
    <Link href={href} className="rounded-card border border-edge bg-surface p-5 shadow-raised hover:border-violet/40">
      <div className="flex items-center justify-between"><p className="text-sm font-medium text-ink-soft">{label}</p><span className="flex h-9 w-9 items-center justify-center rounded-chip bg-violet-soft text-violet"><Icon name={icon} /></span></div>
      <p className="mt-4 text-3xl font-semibold tracking-tight text-ink" aria-label={value === null ? `${label}: unknown` : undefined}>{value === null ? "—" : value}</p>
      {hint && <p className="mt-1 text-xs text-ink-soft">{hint}</p>}
    </Link>
  );
}
