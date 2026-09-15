import { AppShell } from "@/components/AppShell";
import { ActivityItem } from "@/components/ActivityItem";
import { DemoDataNotice } from "@/components/DemoDataNotice";
import { FitDistribution } from "@/components/FitDistribution";
import { JobRow } from "@/components/JobRow";
import { MetricCard } from "@/components/MetricCard";
import { PipelineTrack } from "@/components/PipelineTrack";
import { ProvenanceTag } from "@/components/ProvenanceTag";
import { ReviewAlert } from "@/components/ReviewAlert";
import { SectionHeader } from "@/components/SectionHeader";
import {
  DEMO_ACTIVITY,
  DEMO_ATTENTION,
  DEMO_FIT_DISTRIBUTION,
  DEMO_JOBS,
  DEMO_METRICS,
  DEMO_PIPELINE,
} from "@/lib/demo-data";

export default function OverviewPage() {
  return (
    <AppShell
      title="Good morning, Likhith"
      description="31 strong matches from 214 jobs. Four are waiting on you."
    >
      <div id="content" className="mx-auto max-w-[84rem] space-y-14">
        <DemoDataNotice />

        {/* KPI row */}
        <section aria-label="Today at a glance">
          <div className="grid gap-5 sm:grid-cols-2 xl:grid-cols-4">
            <MetricCard
              label="Jobs discovered"
              value={DEMO_METRICS.discovered}
              hint="From 3 sources in the last 7 days"
              icon="feed"
            />
            <MetricCard
              label="Strong matches"
              value={DEMO_METRICS.strongMatches}
              hint="Scored 75 or higher"
              icon="overview"
            />
            <MetricCard
              label="Waiting on you"
              value={DEMO_METRICS.awaitingApproval}
              hint="Nothing is sent until you say so"
              icon="approvals"
              tone="review"
            />
            <MetricCard
              label="Applications sent"
              value={DEMO_METRICS.applicationsSent}
              hint={`${DEMO_METRICS.dailyQuota} a day maximum`}
              icon="applications"
            />
          </div>
        </section>

        {/* Strong matches */}
        <section aria-labelledby="matches-heading">
          <SectionHeader
            id="matches-heading"
            title="Worth your time today"
            description="Ranked by how much of what each employer asked for you can already show."
            action={
              <button
                type="button"
                className="rounded-chip border border-edge bg-surface px-3 py-1.5 text-sm text-ink hover:border-brass hover:text-brass-deep"
              >
                Open Job Feed
              </button>
            }
          />
          <div className="overflow-hidden rounded-card border border-edge bg-surface">
            <div className="overflow-x-auto">
              <table className="w-full min-w-[52rem] text-left text-sm">
                <caption className="sr-only">
                  Recommended roles with fit score, status and when they were found
                </caption>
                <thead>
                  <tr className="border-b border-edge bg-muted/40 text-xs text-ink-soft">
                    <th scope="col" className="px-5 py-3 font-medium">Role and company</th>
                    <th scope="col" className="px-5 py-3 font-medium">Fit</th>
                    <th scope="col" className="hidden px-5 py-3 font-medium md:table-cell">Why</th>
                    <th scope="col" className="px-5 py-3 font-medium">Status</th>
                    <th scope="col" className="hidden px-5 py-3 font-medium lg:table-cell">Found</th>
                    <th scope="col" className="px-5 py-3 text-right font-medium">Next step</th>
                  </tr>
                </thead>
                <tbody>
                  {DEMO_JOBS.map((job) => (
                    <JobRow key={job.id} job={job} />
                  ))}
                </tbody>
              </table>
            </div>
            <div className="flex flex-wrap items-center gap-2 border-t border-edge bg-muted/30 px-5 py-3">
              <ProvenanceTag kind="deterministic" />
              <p className="text-xs text-ink-soft">
                Scores are counted, not guessed. The same job always gets the
                same score, and you can see what went into it.
              </p>
            </div>
          </div>
        </section>

        {/* Pipeline */}
        <section aria-labelledby="pipeline-heading">
          <SectionHeader
            id="pipeline-heading"
            title="Where everything stands"
            description="Every job moves left to right. Only one stage needs you."
          />
          <PipelineTrack stages={DEMO_PIPELINE} />
        </section>

        {/* Activity + attention */}
        <div className="grid gap-10 lg:grid-cols-[1.5fr_1fr]">
          <section aria-labelledby="activity-heading">
            <SectionHeader
              id="activity-heading"
              title="What JobForge did"
              description="Each entry says whether it was worked out by fixed rules or written by a model."
            />
            <div className="rounded-card border border-edge bg-surface px-5 py-1 shadow-raised">
              <ul className="divide-y divide-edge">
                {DEMO_ACTIVITY.map((item) => (
                  <ActivityItem key={item.id} item={item} />
                ))}
              </ul>
            </div>
          </section>

          <section aria-labelledby="attention-heading">
            <SectionHeader
              id="attention-heading"
              title="Needs a decision"
              description="Three things nobody else can decide for you."
            />
            <div className="space-y-4">
              {DEMO_ATTENTION.map((item) => (
                <ReviewAlert key={item.id} item={item} />
              ))}
            </div>
          </section>
        </div>

        {/* Fit distribution */}
        <section aria-labelledby="fit-heading" className="max-w-2xl">
          <SectionHeader
            id="fit-heading"
            title="How your last 96 matches scored"
            description="Lots of low scores usually means the search is too wide, not that you are a weak candidate."
          />
          <div className="rounded-card border border-edge bg-surface p-6 shadow-raised">
            <FitDistribution bands={DEMO_FIT_DISTRIBUTION} />
          </div>
        </section>
      </div>
    </AppShell>
  );
}
