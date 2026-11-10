"use client";

import Link from "next/link";
import { use, useEffect, useState } from "react";
import { AppShell } from "@/components/AppShell";
import { Icon } from "@/components/Icon";
import { FirstDraftPanel } from "@/components/FirstDraftPanel";
import { NotWiredNotice } from "@/components/NotWiredNotice";
import { ProvenanceTag } from "@/components/ProvenanceTag";
import { RequirementList } from "@/components/RequirementList";
import { Skeleton } from "@/components/Skeleton";
import { StatusBadge } from "@/components/StatusBadge";
import { api, type ApiJobDetail } from "@/lib/api";
import {
  STATUS_LABEL,
  WORK_MODE_LABEL,
  formatLocation,
  formatSalary,
  relativeTime,
  splitRequirements,
  workMode,
} from "@/lib/jobs";

export default function JobDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  const [job, setJob] = useState<ApiJobDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    api
      .getJob(id)
      .then((result) => {
        if (!cancelled) setJob(result);
      })
      .catch((cause) => {
        if (!cancelled) {
          setError(cause instanceof Error ? cause.message : "Could not load this job");
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [id]);

  const backLink = (
    <Link
      href="/job-feed"
      className="inline-flex items-center gap-1.5 text-sm font-medium text-ink-soft hover:text-ink"
    >
      <span aria-hidden="true">←</span> Back to Job Feed
    </Link>
  );

  if (loading) {
    return (
      <AppShell title="Loading job" description="Fetching the posting.">
        <div id="content" className="mx-auto max-w-[84rem] space-y-4">
          <Skeleton className="h-6 w-80" />
          <Skeleton className="h-4 w-56" />
          <Skeleton className="h-64 w-full" />
        </div>
      </AppShell>
    );
  }

  if (error || !job) {
    return (
      <AppShell title="Job not found" description="This posting could not be loaded.">
        <div id="content" className="mx-auto max-w-2xl space-y-4 pt-6">
          <p role="alert" className="rounded-card border border-edge bg-surface px-4 py-3 text-sm text-danger">
            {error ?? "No job with that id."}
          </p>
          {backLink}
        </div>
      </AppShell>
    );
  }

  const { required, preferred } = splitRequirements(job.requirements?.requirements);
  const totalRequirements = required.length + preferred.length;
  const mode = workMode(job);
  const salary = formatSalary(job);

  return (
    <AppShell
      title={job.title}
      description={`${formatLocation(job)} · ${WORK_MODE_LABEL[mode]}`}
    >
      <div id="content" className="mx-auto max-w-[84rem] space-y-6">
        {backLink}

        {/* Header */}
        <header className="rounded-card border border-edge bg-surface p-5 shadow-raised">
          <div className="flex flex-wrap items-start justify-between gap-4">
            <div className="min-w-0">
              <h2 className="text-display font-semibold text-ink">{job.title}</h2>
              <p className="mt-1.5 flex flex-wrap items-center gap-x-2.5 gap-y-1 text-sm text-ink-soft">
                <span>{formatLocation(job)}</span>
                <span aria-hidden="true">·</span>
                <span>{WORK_MODE_LABEL[mode]}</span>
                {job.seniority && job.seniority !== "unknown" && (
                  <>
                    <span aria-hidden="true">·</span>
                    <span className="capitalize">{job.seniority}</span>
                  </>
                )}
                {salary && (
                  <>
                    <span aria-hidden="true">·</span>
                    <span className="tabular">{salary}</span>
                  </>
                )}
                <span aria-hidden="true">·</span>
                <span>Found {relativeTime(job.first_seen_at)}</span>
              </p>
            </div>
            <div className="flex flex-col items-end gap-2">
              <StatusBadge label={STATUS_LABEL[job.status] ?? job.status} />
              <a
                href={job.url}
                target="_blank"
                rel="noreferrer noopener"
                className="inline-flex items-center gap-1.5 rounded-chip border border-edge bg-surface px-3 py-1.5 text-sm font-medium text-ink hover:border-control hover:text-attention"
              >
                View original posting
                <Icon name="arrow" className="h-3.5 w-3.5" />
              </a>
            </div>
          </div>
        </header>

        <div className="grid gap-6 lg:grid-cols-[1.6fr_1fr]">
          {/* Left: description + requirements */}
          <div className="space-y-6">
            <section aria-labelledby="requirements-heading">
              <h2 id="requirements-heading" className="sr-only">
                What this job asks for
              </h2>
              {totalRequirements === 0 ? (
                <NotWiredNotice
                  title="No requirements have been read from this posting"
                  body="JobForge can break a description into required and preferred items, but nothing has run that step against this job yet."
                  needs="the ingestion pipeline to call A1 and store jobs.requirements"
                />
              ) : (
                <div className="space-y-5">
                  <RequirementList
                    title="Must have"
                    requirements={required}
                    emptyNote="Nothing was listed as essential."
                  />
                  <RequirementList
                    title="Nice to have"
                    requirements={preferred}
                    emptyNote="Nothing was listed as preferred."
                  />
                  <p className="flex items-center gap-2 text-xs text-ink-soft">
                    <ProvenanceTag kind="deterministic" />
                    Read straight from the posting by fixed rules. Each line is a
                    quote, not a summary.
                  </p>
                </div>
              )}
            </section>

            <section aria-labelledby="description-heading">
              <h2 id="description-heading" className="pb-2 text-sm font-medium text-ink">
                The posting, as written
              </h2>
              <div className="rounded-card border border-edge bg-surface p-5">
                <p className="mb-3 text-xs text-ink-soft">
                  This is text from an outside website. JobForge reads it as
                  content, never as instructions.
                </p>
                {job.description_raw ? (
                  <div className="max-h-[32rem] overflow-y-auto whitespace-pre-wrap text-sm leading-relaxed text-ink">
                    {job.description_raw}
                  </div>
                ) : (
                  <p className="text-sm text-ink-soft">
                    No description was captured for this posting.
                  </p>
                )}
              </div>
            </section>
          </div>

          {/* Right: analysis */}
          <aside className="space-y-4">
            <div className="rounded-card border border-edge bg-surface p-5 shadow-raised">
              <h2 className="text-sm font-medium text-ink">At a glance</h2>
              <dl className="mt-4 space-y-3 text-sm">
                <div className="flex items-baseline justify-between gap-3">
                  <dt className="text-ink-soft">Status</dt>
                  <dd>
                    <StatusBadge label={STATUS_LABEL[job.status] ?? job.status} />
                  </dd>
                </div>
                <div className="flex items-baseline justify-between gap-3">
                  <dt className="text-ink-soft">Must have</dt>
                  <dd className="tabular font-medium text-ink">{required.length}</dd>
                </div>
                <div className="flex items-baseline justify-between gap-3">
                  <dt className="text-ink-soft">Nice to have</dt>
                  <dd className="tabular font-medium text-ink">{preferred.length}</dd>
                </div>
                <div className="flex items-baseline justify-between gap-3">
                  <dt className="text-ink-soft">Work mode</dt>
                  <dd className="font-medium text-ink">{WORK_MODE_LABEL[mode]}</dd>
                </div>
                <div className="flex items-baseline justify-between gap-3">
                  <dt className="text-ink-soft">Found</dt>
                  <dd className="font-medium text-ink">
                    {relativeTime(job.first_seen_at)}
                  </dd>
                </div>
              </dl>
              <div className="mt-4 flex items-center gap-2 border-t border-edge pt-3">
                <ProvenanceTag kind="deterministic" />
                <span className="text-xs text-ink-soft">
                  Everything above is read or counted, not generated.
                </span>
              </div>
            </div>

            <NotWiredNotice
              title="Fit score not available"
              body="Nothing here scores this job against your profile yet. JobForge can calculate a fit score and work out which requirements you meet, but no endpoint returns that, so showing a number would be making one up."
              needs="GET /api/v1/jobs/{id}/evaluation"
            />

            <NotWiredNotice
              title="Evidence from your profile not available"
              body="Matching each requirement to the verified facts that support it is the step that has not been built. Nothing is hidden here — the data does not exist."
              needs="requirement-to-fact mapping (A2)"
            />

            <div className="rounded-card border border-edge bg-surface p-5">
              <h2 className="text-sm font-medium text-ink">What you can do next</h2>
              <ul className="mt-3 space-y-2 text-sm text-ink-soft">
                <li>Read the requirements above and decide if it is worth applying.</li>
                <li>Open the original posting to check anything missing here.</li>
              </ul>
            </div>

            <FirstDraftPanel jobId={job.id} />
          </aside>
        </div>
      </div>
    </AppShell>
  );
}
