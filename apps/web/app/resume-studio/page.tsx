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
      title="Resume Studio"
      description="Tailored resumes, with every line traced to a verified fact."
    >
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
        <ul className="divide-y divide-edge rounded-card border border-edge bg-surface">
          {applications.map((application) => {
            const hasResume = application.latest_resume_version !== null;
            return (
              <li key={application.id} className="flex flex-wrap items-center gap-4 p-4">
                <div className="min-w-0 flex-1">
                  <p className="truncate text-sm font-medium text-ink">
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
                </div>
                <StatusBadge
                  label={applicationLabel(application.status)}
                  tone={applicationTone(application.status)}
                />
                {application.latest_verification_status && (
                  <StatusBadge
                    label={VERIFICATION_LABEL[application.latest_verification_status]}
                    tone={VERIFICATION_TONE[application.latest_verification_status]}
                  />
                )}
                <Link
                  href={`/resume-studio/${application.id}`}
                  className="rounded-chip border border-edge px-3 py-1.5 text-xs font-medium text-ink hover:bg-muted"
                >
                  View resume
                </Link>
              </li>
            );
          })}
        </ul>
      )}
    </AppShell>
  );
}
