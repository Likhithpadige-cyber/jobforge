"use client";

/**
 * Resume Studio detail — the latest persisted resume for one application.
 *
 * Five states, all real: loading, no resume yet, verified, failed
 * verification, and API error. A 404 from the endpoint means either the
 * application does not exist or no resume has been generated for it; both are
 * shown as an empty state rather than an error, because neither is a fault.
 */

import Link from "next/link";
import { use, useEffect, useState } from "react";
import { AppShell } from "@/components/AppShell";
import { EmptyState } from "@/components/EmptyState";
import { ResumeStudioView } from "@/components/ResumeStudioView";
import { Skeleton } from "@/components/Skeleton";
import {
  ApiError,
  api,
  type ApiResume,
  type ApiResumeReview,
} from "@/lib/api";

export default function Page({
  params,
}: {
  params: Promise<{ applicationId: string }>;
}) {
  const { applicationId } = use(params);
  const [resume, setResume] = useState<ApiResume | null>(null);
  const [missing, setMissing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [revision, setRevision] = useState(0);

  useEffect(() => {
    let cancelled = false;
    api
      .latestResume(applicationId)
      .then((result) => {
        if (!cancelled) setResume(result);
      })
      .catch((cause) => {
        if (cancelled) return;
        if (cause instanceof ApiError && cause.status === 404) {
          setMissing(true);
          return;
        }
        setError(cause instanceof Error ? cause.message : "Could not load this resume");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [applicationId, revision]);

  return (
    <AppShell
      title="Resume Revision"
      description="Review the latest persisted version and its verified claims."
    >
      <div id="content" className="mx-auto max-w-[84rem]">
      <nav aria-label="Breadcrumb" className="mb-5 flex flex-wrap items-center gap-2 text-sm text-ink-soft"><Link href="/applications" className="hover:text-accent">Applications</Link><span aria-hidden="true">›</span><Link href={`/applications/${applicationId}`} className="hover:text-accent">{resume?.job_title && resume?.job_company ? `${resume.job_title} at ${resume.job_company}` : "Application"}</Link><span aria-hidden="true">›</span><span className="text-ink">Resume revision</span></nav>

      {loading && (
        <div className="space-y-4" data-testid="loading">
          <Skeleton className="h-20 w-full" />
          <Skeleton className="h-40 w-full" />
          <Skeleton className="h-40 w-full" />
        </div>
      )}

      {!loading && missing && (
        <EmptyState
          title="No resume generated yet"
          body="Nothing has been persisted for this application. Once the tailoring pipeline runs, the resume and its verified claims appear here."
        />
      )}

      {!loading && error && (
        <EmptyState
          title="Could not load this resume"
          body={`${error}. Check that the API is running, then reload.`}
        />
      )}

      {!loading && resume && (
        <ResumeStudioView
          resume={resume}
          onReviewed={(next: ApiResumeReview) => {
            if (next.resume_id !== resume.id) {
              // Hide the old text and actions until the new verified claims load.
              setLoading(true);
              setResume(null);
              setError(null);
              setMissing(false);
              setRevision((value) => value + 1);
              return;
            }
            setResume((current) => current ? {
              ...current,
              application_status: next.application_status,
              review: next,
            } : current);
          }}
        />
      )}
      </div>
    </AppShell>
  );
}
