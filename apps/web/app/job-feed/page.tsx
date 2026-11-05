"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { AppShell } from "@/components/AppShell";
import { JobFeedTable } from "@/components/JobFeedTable";
import { NotWiredNotice } from "@/components/NotWiredNotice";
import { SectionHeader } from "@/components/SectionHeader";
import { api, type ApiJob, type ApiSource, type JobStatus } from "@/lib/api";
import {
  DEFAULT_FILTERS,
  type FeedFilters,
  applyFilters,
  filtersAreDefault,
} from "@/lib/jobs";

const PAGE_SIZE = 100;

const STATUS_OPTIONS: { value: JobStatus | "all"; label: string }[] = [
  { value: "all", label: "Any status" },
  { value: "normalized", label: "Ready to score" },
  { value: "evaluated", label: "Needs your call" },
  { value: "shortlisted", label: "Shortlisted" },
  { value: "applied", label: "Applied" },
  { value: "discarded", label: "Set aside" },
];

const SORT_OPTIONS = [
  { value: "best-fit", label: "Best available" },
  { value: "newest", label: "Newest" },
  { value: "company", label: "Role A–Z" },
  { value: "status", label: "Status" },
] as const;

const selectClass =
  "h-9 rounded-chip border border-edge bg-surface px-2.5 text-sm text-ink";

export default function JobFeedPage() {
  const [jobs, setJobs] = useState<ApiJob[]>([]);
  const [sources, setSources] = useState<ApiSource[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [offset, setOffset] = useState(0);
  const [exhausted, setExhausted] = useState(false);
  const [filters, setFilters] = useState<FeedFilters>(DEFAULT_FILTERS);

  const load = useCallback(async (nextOffset: number, replace: boolean) => {
    setLoading(true);
    try {
      const page = await api.listJobs({ limit: PAGE_SIZE, offset: nextOffset });
      setJobs((current) => (replace ? page : [...current, ...page]));
      setExhausted(page.length < PAGE_SIZE);
      setError(null);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not load jobs");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load(0, true);
    api.listSources().then(setSources).catch(() => setSources([]));
  }, [load]);

  const sourceNames = useMemo(
    () => Object.fromEntries(sources.map((s) => [s.id, s.name])),
    [sources],
  );

  const visible = useMemo(() => applyFilters(jobs, filters), [jobs, filters]);
  const dirty = !filtersAreDefault(filters);
  const update = <K extends keyof FeedFilters>(key: K, value: FeedFilters[K]) =>
    setFilters((current) => ({ ...current, [key]: value }));

  return (
    <AppShell
      title="Job Feed"
      description="Everything JobForge has found. Open a job to see what the employer asked for."
    >
      <div id="content" className="mx-auto max-w-[84rem] space-y-6">
        <SectionHeader
          title={
            loading && jobs.length === 0
              ? "Loading jobs"
              : `${visible.length} ${visible.length === 1 ? "job" : "jobs"}`
          }
          description={
            dirty
              ? `Filtered from ${jobs.length} loaded.`
              : "Sorted by the order the pipeline ranked them."
          }
          action={
            dirty ? (
              <button
                type="button"
                onClick={() => setFilters(DEFAULT_FILTERS)}
                className="rounded-chip border border-edge bg-surface px-3 py-1.5 text-sm font-medium text-ink hover:border-control hover:text-attention"
              >
                Clear filters
              </button>
            ) : undefined
          }
        />

        {/* Filters */}
        <div className="flex flex-wrap items-center gap-2.5 rounded-card border border-edge bg-surface p-3">
          <label htmlFor="feed-search" className="sr-only">
            Search by role, location or seniority
          </label>
          <input
            id="feed-search"
            type="search"
            value={filters.search}
            onChange={(event) => update("search", event.target.value)}
            placeholder="Search roles, locations"
            className="h-9 min-w-[14rem] flex-1 rounded-chip border border-edge bg-surface px-3 text-sm text-ink placeholder:text-ink-soft"
          />

          <label htmlFor="feed-status" className="sr-only">Status</label>
          <select
            id="feed-status"
            value={filters.status}
            onChange={(event) => update("status", event.target.value as FeedFilters["status"])}
            className={selectClass}
          >
            {STATUS_OPTIONS.map((option) => (
              <option key={option.value} value={option.value}>{option.label}</option>
            ))}
          </select>

          <label htmlFor="feed-mode" className="sr-only">Work mode</label>
          <select
            id="feed-mode"
            value={filters.mode}
            onChange={(event) => update("mode", event.target.value as FeedFilters["mode"])}
            className={selectClass}
          >
            <option value="all">Anywhere</option>
            <option value="remote">Remote</option>
            <option value="hybrid">Hybrid</option>
            <option value="onsite">On-site</option>
          </select>

          <label htmlFor="feed-source" className="sr-only">Source</label>
          <select
            id="feed-source"
            value={filters.sourceId}
            onChange={(event) => update("sourceId", event.target.value)}
            className={selectClass}
          >
            <option value="all">Any source</option>
            {sources.map((source) => (
              <option key={source.id} value={source.id}>{source.name}</option>
            ))}
          </select>

          <label htmlFor="feed-sort" className="sr-only">Sort by</label>
          <select
            id="feed-sort"
            value={filters.sort}
            onChange={(event) => update("sort", event.target.value as FeedFilters["sort"])}
            className={`${selectClass} ml-auto`}
          >
            {SORT_OPTIONS.map((option) => (
              <option key={option.value} value={option.value}>Sort: {option.label}</option>
            ))}
          </select>
        </div>

        {error && (
          <p role="alert" className="rounded-card border border-edge bg-surface px-4 py-3 text-sm text-danger">
            {error}
          </p>
        )}

        <JobFeedTable
          jobs={visible}
          sourceNames={sourceNames}
          loading={loading && jobs.length === 0}
          hasFilters={dirty}
          onResetFilters={() => setFilters(DEFAULT_FILTERS)}
        />

        {!exhausted && jobs.length > 0 && (
          <div className="flex justify-center">
            <button
              type="button"
              disabled={loading}
              onClick={() => {
                const next = offset + PAGE_SIZE;
                setOffset(next);
                void load(next, false);
              }}
              className="rounded-chip border border-edge bg-surface px-4 py-2 text-sm font-medium text-ink hover:border-control hover:text-attention"
            >
              {loading ? "Loading…" : "Load more"}
            </button>
          </div>
        )}

        <NotWiredNotice
          title="Fit scores are not in this list yet"
          body="Search, filters and sorting run over the jobs already loaded, not on the server, and no fit score is available to rank by. Both need backend work before this list can order by match quality."
          needs="search/sort params on GET /jobs, and GET /jobs/{id}/evaluation"
        />
      </div>
    </AppShell>
  );
}
