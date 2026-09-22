"use client";

/**
 * Resume Studio index.
 *
 * Lists applications so a resume can be opened without typing a UUID. Real
 * API data only — there is no fixture resume behind this page.
 */

import Link from "next/link";
import { useEffect, useState } from "react";
import { AppShell } from "@/components/AppShell";
import { EmptyState } from "@/components/EmptyState";
import { SkeletonRow } from "@/components/Skeleton";
import { StatusBadge } from "@/components/StatusBadge";
import { api, type ApiApplication } from "@/lib/api";
import {
  VERIFICATION_LABEL,
  VERIFICATION_TONE,
  applicationLabel,
  applicationTone,
  formatTimestamp,
} from "@/lib/resumes";

export default function Page() {
  const [applications, setApplications] = useState<ApiApplication[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    api
      .listApplications()
      .then((result) => {
        if (!cancelled) setApplications(result);
      })
      .catch((cause) => {
        if (!cancelled) {
          setError(
            cause instanceof Error ? cause.message : "Could not load applications",
          );
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <AppShell
      title="Resume"
      description="Your documents and the verified versions prepared for each role."
    >
      <div id="content" className="mx-auto max-w-[84rem] space-y-7">
      <section className="reveal rounded-[1.5rem] border border-violet/10 bg-gradient-to-br from-white to-[#F6F2FA] p-6 shadow-raised sm:p-8">
        <p className="text-xs font-semibold uppercase tracking-[.14em] text-violet-deep">Your documents</p>
        <h2 className="mt-2 text-3xl font-semibold tracking-tight text-ink">Every version, with its purpose clear.</h2>
        <p className="mt-2 max-w-2xl text-sm leading-6 text-ink-soft">Review tailored resumes by role and see what JobForge has verified before you approve them.</p>
      </section>

      <section aria-labelledby="master-heading" className="reveal rounded-card border border-edge bg-surface p-6 shadow-raised">
        <div className="flex flex-wrap items-start justify-between gap-4"><div><p className="text-xs font-semibold uppercase tracking-[.12em] text-brass-deep">Foundation</p><h3 id="master-heading" className="mt-1 text-xl font-semibold text-ink">Master Resume</h3><p className="mt-2 text-sm text-ink-soft">A master resume record is not available from the current API. Tailored versions below remain available.</p></div><StatusBadge label="Not connected" tone="neutral" /></div>
      </section>

      <div className="flex flex-wrap items-end justify-between gap-2"><div><h3 className="text-xl font-semibold text-ink">Tailored Resumes</h3><p className="mt-1 text-sm text-ink-soft">Latest persisted version for each application.</p></div><p className="text-xs text-ink-soft">{applications?.length ?? 0} applications loaded</p></div>
      {loading && (
        <div className="rounded-card border border-edge bg-surface" data-testid="loading">
          <SkeletonRow />
          <SkeletonRow />
          <SkeletonRow />
        </div>
      )}

      {!loading && error && (
        <EmptyState
          title="Could not reach the API"
          body={`${error}. Check that the API is running, then reload.`}
        />
      )}

      {!loading && !error && applications?.length === 0 && (
        <EmptyState
          title="No applications yet"
          body="Once an application exists and a resume has been generated for it, it will appear here."
        />
      )}

      {!loading && !error && applications && applications.length > 0 && (
        <ul className="stagger grid gap-4 md:grid-cols-2">
          {applications.map((application) => {
            const hasResume = application.latest_resume_version !== null;
            return (
              <li key={application.id} className="reveal lift rounded-[1.25rem] border border-edge bg-surface p-5 shadow-raised sm:p-6">
                <div className="flex flex-wrap items-start justify-between gap-3"><div className="min-w-0 flex-1">
                  <p className="text-xs font-semibold uppercase tracking-[.12em] text-violet-deep">{application.job_company ?? "Company unavailable"}</p>
                  <p className="mt-1 truncate text-lg font-semibold text-ink">
                    {[application.job_title, application.job_company]
                      .filter(Boolean)
                      .join(" · ") || "Untitled role"}
                  </p>
                  <p className="mt-0.5 text-xs text-ink-soft">
                    {hasResume
                      ? `v${application.latest_resume_version} · ${formatTimestamp(
                          application.updated_at,
                        )}`
                      : `No resume generated · ${formatTimestamp(application.updated_at)}`}
                  </p>
                </div><StatusBadge label={applicationLabel(application.status)} tone={applicationTone(application.status)} /></div>
                <div className="mt-5 flex flex-wrap gap-2">
                {application.latest_verification_status && (
                  <StatusBadge
                    label={VERIFICATION_LABEL[application.latest_verification_status]}
                    tone={VERIFICATION_TONE[application.latest_verification_status]}
                  />
                )}</div>
                <Link
                  href={`/resume-studio/${application.id}`}
                  className="mt-5 inline-flex min-h-11 items-center rounded-chip border border-violet/20 bg-violet-soft px-4 text-sm font-semibold text-violet-deep hover:border-violet"
                >
                  {hasResume ? "Review resume" : "View status"} <span aria-hidden="true" className="ml-2">→</span>
                </Link>
              </li>
            );
          })}
        </ul>
      )}
      <section aria-labelledby="history-heading" className="reveal rounded-card border border-edge bg-muted/50 p-6"><h3 id="history-heading" className="text-lg font-semibold text-ink">Revision History</h3><p className="mt-2 text-sm text-ink-soft">The application list reports how many tailored resumes exist, but the current API does not list earlier versions or their content. Open a role to review its latest version.</p></section>
      </div>
    </AppShell>
  );
}
