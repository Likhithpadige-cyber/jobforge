import Link from "next/link";
import { AppShell } from "@/components/AppShell";
import { ActivityItem } from "@/components/ActivityItem";
import { AnimatedNumber } from "@/components/AnimatedNumber";
import { DashboardGreeting } from "@/components/DashboardGreeting";
import { DemoDataNotice } from "@/components/DemoDataNotice";
import { FitDistribution } from "@/components/FitDistribution";
import { Icon } from "@/components/Icon";
import { JobRow } from "@/components/JobRow";
import { PipelineTrack } from "@/components/PipelineTrack";
import { ProvenanceTag } from "@/components/ProvenanceTag";
import { ReviewAlert } from "@/components/ReviewAlert";
import { SectionHeader } from "@/components/SectionHeader";
import { DEMO_ACTIVITY, DEMO_ATTENTION, DEMO_FIT_DISTRIBUTION, DEMO_JOBS, DEMO_METRICS, DEMO_PIPELINE } from "@/lib/demo-data";
import { getDashboardPriority } from "@/lib/dashboard-priority";

export default function OverviewPage() {
  const priority = getDashboardPriority({ approvalsWaiting: DEMO_METRICS.awaitingApproval, strongMatches: DEMO_METRICS.strongMatches });
  return <AppShell title="Dashboard" description="A clear view of what matters next in your career search.">
    <div id="content" className="mx-auto max-w-[84rem] space-y-10">
      <DemoDataNotice />
      <section className="reveal relative overflow-hidden rounded-[1.5rem] border border-violet/10 bg-gradient-to-br from-white via-[#FBFAFF] to-[#F3F0FA] px-6 py-8 shadow-raised sm:px-9 sm:py-10" aria-labelledby="welcome-heading">
        <div aria-hidden="true" className="pointer-events-none absolute -right-16 -top-24 h-80 w-80 rounded-full bg-violet/5 blur-3xl" />
        <div className="relative grid items-center gap-8 lg:grid-cols-[1.35fr_.65fr]">
          <div>
            <p className="mb-3 text-xs font-semibold uppercase tracking-[0.16em] text-violet-deep">Your career, in motion</p>
            <h2 id="welcome-heading" className="max-w-2xl text-3xl font-semibold leading-tight tracking-[-.035em] text-ink sm:text-4xl"><DashboardGreeting name="Likhith" /><br /><span className="text-violet-deep">Let’s make your next move count.</span></h2>
            <p className="mt-4 max-w-xl text-sm leading-7 text-ink-soft">JobForge found promising roles, prepared the next steps, and kept every decision in your hands.</p>
          </div>
          <div className="lift rounded-card border border-violet/10 bg-white/90 p-5 shadow-raised">
            <div className="mb-4 flex items-center gap-2 text-xs font-semibold text-violet-deep"><span className="flex h-7 w-7 items-center justify-center rounded-full bg-violet-soft"><Icon name={priority.icon} /></span>{priority.eyebrow}</div>
            <h3 className="text-lg font-semibold leading-snug text-ink">{priority.title}</h3>
            <p className="mt-2 text-sm leading-6 text-ink-soft">{priority.body}</p>
            <Link href={priority.href} className="mt-5 inline-flex items-center gap-2 rounded-chip bg-violet px-4 py-2.5 text-sm font-semibold text-white transition-[transform,background-color] hover:-translate-y-1 hover:bg-violet-deep">{priority.action}<Icon name="arrow" /></Link>
          </div>
        </div>
      </section>

      <section aria-label="Today at a glance" className="stagger grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        {[
          { label: "Jobs discovered", value: DEMO_METRICS.discovered, hint: "From 3 sources in the last 7 days", icon: "feed" as const },
          { label: "Strong matches", value: DEMO_METRICS.strongMatches, hint: "Scored 75 or higher", icon: "spark" as const },
          { label: "Waiting on you", value: DEMO_METRICS.awaitingApproval, hint: "Nothing is sent until you approve", icon: "approvals" as const },
          { label: "Applications sent", value: DEMO_METRICS.applicationsSent, hint: `${DEMO_METRICS.dailyQuota} a day maximum`, icon: "applications" as const },
        ].map(metric => <div key={metric.label} className="reveal lift rounded-card border border-edge bg-surface p-5 shadow-raised"><div className="flex items-center justify-between"><p className="text-sm font-medium text-ink-soft">{metric.label}</p><span className="flex h-9 w-9 items-center justify-center rounded-chip bg-violet-soft text-violet"><Icon name={metric.icon} /></span></div><p className="mt-4 text-3xl font-semibold tracking-tight text-ink"><AnimatedNumber value={metric.value} /></p><p className="mt-1 text-xs text-ink-soft">{metric.hint}</p></div>)}
      </section>

      <section aria-labelledby="matches-heading" className="reveal">
        <SectionHeader id="matches-heading" title="Worth your time today" description="Roles ranked by how closely your verified experience matches what employers ask for." action={<Link href="/job-feed" className="inline-flex items-center gap-2 text-sm font-semibold text-violet-deep hover:underline">View all jobs <Icon name="arrow" /></Link>} />
        <div className="overflow-hidden rounded-card border border-edge bg-surface shadow-raised"><div className="overflow-x-auto"><table className="w-full min-w-[52rem] text-left text-sm"><caption className="sr-only">Recommended roles with fit score, status and when they were found</caption><thead><tr className="border-b border-edge bg-muted/40 text-xs text-ink-soft"><th scope="col" className="px-5 py-3 font-medium">Role and company</th><th scope="col" className="px-5 py-3 font-medium">Fit</th><th scope="col" className="hidden px-5 py-3 font-medium md:table-cell">Why</th><th scope="col" className="px-5 py-3 font-medium">Status</th><th scope="col" className="hidden px-5 py-3 font-medium lg:table-cell">Found</th><th scope="col" className="px-5 py-3 text-right font-medium">Next step</th></tr></thead><tbody>{DEMO_JOBS.slice(0, 4).map(job => <JobRow key={job.id} job={job} />)}</tbody></table></div><div className="flex flex-wrap items-center gap-2 border-t border-edge bg-muted/30 px-5 py-3"><ProvenanceTag kind="deterministic" /><p className="text-xs text-ink-soft">Scores follow fixed rules you can inspect.</p></div></div>
      </section>

      <section aria-labelledby="pipeline-heading" className="reveal"><SectionHeader id="pipeline-heading" title="Your application journey" description="Follow each opportunity from discovery through interview." /><PipelineTrack stages={DEMO_PIPELINE} /></section>
      <div className="grid gap-9 lg:grid-cols-[1.25fr_.75fr]">
        <section aria-labelledby="activity-heading" className="reveal"><SectionHeader id="activity-heading" title="Recent activity" description="A transparent record of what JobForge did for you." /><div className="rounded-card border border-edge bg-surface px-5 py-1 shadow-raised"><ul className="divide-y divide-edge">{DEMO_ACTIVITY.slice(0, 4).map(item => <ActivityItem key={item.id} item={item} />)}</ul></div></section>
        <section aria-labelledby="attention-heading" className="reveal"><SectionHeader id="attention-heading" title="Needs a decision" description="The important calls stay with you." /><div className="space-y-3">{DEMO_ATTENTION.map(item => <ReviewAlert key={item.id} item={item} />)}</div></section>
      </div>
      <section aria-labelledby="fit-heading" className="reveal max-w-2xl"><SectionHeader id="fit-heading" title="Match quality" description="A quick look at how recent roles fit your experience." /><div className="rounded-card border border-edge bg-surface p-6 shadow-raised"><FitDistribution bands={DEMO_FIT_DISTRIBUTION} /></div></section>
    </div>
  </AppShell>;
}
