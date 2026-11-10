"use client";

/**
 * Owner-only manual job import for the Job Feed.
 *
 * Two inputs, one server path: a pasted posting is serialised as a one-item
 * JSON array, and an uploaded CSV/JSON file is sent as-is. Both go to
 * POST /api/v1/jobs/import, which parses, validates, de-duplicates and
 * reports every posting's outcome. Nothing here fetches a URL - the link is
 * stored as the posting's identity, never visited - and the checks below are
 * only early feedback; the server remains the authority.
 */

import Link from "next/link";
import { useState } from "react";
import { useAuth } from "@/components/AuthGuard";
import { api, apiErrorText, type ApiSource, type ImportJobsResponse } from "@/lib/api";

/** Mirrors ImportRequest.content's 5,000,000-character server limit. */
export const MAX_IMPORT_BYTES = 5_000_000;
const ACCEPTED = [".csv", ".json"] as const;

const FIELD =
  "w-full rounded-chip border border-edge bg-surface px-3 py-2 text-sm text-ink placeholder:text-ink-soft";
const BUTTON =
  "inline-flex min-h-11 items-center rounded-chip border border-edge px-4 py-2 text-sm font-semibold disabled:cursor-not-allowed disabled:opacity-60";

export interface PastedPosting {
  title: string;
  company: string;
  url: string;
  location: string;
  description: string;
}

const EMPTY: PastedPosting = { title: "", company: "", url: "", location: "", description: "" };

/** Early, user-facing checks. Returns one message per problem. */
export function validatePosting(posting: PastedPosting): string[] {
  const problems: string[] = [];
  if (!posting.title.trim()) problems.push("Enter the job title.");
  if (!posting.company.trim()) problems.push("Enter the company name.");
  const url = posting.url.trim();
  if (!url) {
    problems.push("Enter the posting's link. JobForge stores it to recognise duplicates; it never visits it.");
  } else if (!/^https?:\/\/\S+$/i.test(url)) {
    problems.push("The posting link must start with http:// or https:// and contain no spaces.");
  }
  if (posting.description.trim().length < 40) {
    problems.push("Paste the full job description (at least a few sentences) so requirements can be extracted.");
  }
  return problems;
}

/** The one-item JSON body the server's manual JSON parser accepts. */
export function postingToJson(posting: PastedPosting): string {
  const row: Record<string, string> = {
    title: posting.title.trim(),
    company_name: posting.company.trim(),
    url: posting.url.trim(),
    description: posting.description.trim(),
  };
  if (posting.location.trim()) row.location = posting.location.trim();
  return JSON.stringify([row]);
}

export function fileFormat(name: string): "csv" | "json" | null {
  const lower = name.toLowerCase();
  if (lower.endsWith(".csv")) return "csv";
  if (lower.endsWith(".json")) return "json";
  return null;
}


export function JobImportPanel({
  sources,
  onImported,
}: {
  sources: ApiSource[];
  onImported: () => void;
}) {
  const { user } = useAuth();
  const enabled = sources.filter((s) => s.enabled);
  const [open, setOpen] = useState(false);
  const [mode, setMode] = useState<"paste" | "file">("paste");
  const [sourceSlug, setSourceSlug] = useState("");
  const [posting, setPosting] = useState<PastedPosting>(EMPTY);
  const [file, setFile] = useState<File | null>(null);
  const [problems, setProblems] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<ImportJobsResponse | null>(null);

  if (user?.role !== "owner") return null;

  const slug = sourceSlug || enabled[0]?.slug || "";
  const set = (key: keyof PastedPosting) => (value: string) =>
    setPosting((current) => ({ ...current, [key]: value }));

  async function submit() {
    setResult(null);
    const found: string[] = [];
    if (!slug) found.push("No enabled job source is available to record this import against.");
    let format: "csv" | "json" = "json";
    let content = "";
    if (mode === "paste") {
      found.push(...validatePosting(posting));
      content = postingToJson(posting);
    } else if (!file) {
      found.push("Choose a .csv or .json file.");
    } else {
      const detected = fileFormat(file.name);
      if (!detected) found.push("Only .csv and .json files can be imported.");
      if (file.size === 0) found.push("The chosen file is empty.");
      if (file.size > MAX_IMPORT_BYTES) found.push("The file is larger than the 5 MB import limit.");
      if (detected) format = detected;
    }
    setProblems(found);
    if (found.length) return;
    setBusy(true);
    try {
      if (mode === "file" && file) content = await file.text();
      const response = await api.importJobs({ source_slug: slug, format, content });
      setResult(response);
      if (response.created || response.updated) {
        if (mode === "paste") setPosting(EMPTY);
        onImported();
      }
    } catch (cause) {
      setProblems([apiErrorText(cause, "The import failed.")]);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section
      aria-labelledby="job-import-heading"
      className="rounded-card border border-edge bg-surface p-4"
    >
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 id="job-import-heading" className="text-base font-semibold text-ink">
            Add a job
          </h2>
          <p className="mt-1 text-sm text-ink-soft">
            Paste a posting or upload a CSV/JSON export. JobForge never visits the link.
          </p>
        </div>
        <button
          type="button"
          aria-expanded={open}
          aria-controls="job-import-form"
          onClick={() => setOpen((v) => !v)}
          className={`${BUTTON} bg-surface text-ink hover:border-control hover:text-attention`}
        >
          {open ? "Close" : "Add a job"}
        </button>
      </div>

      {open && (
        <form
          id="job-import-form"
          className="mt-4 space-y-4"
          onSubmit={(event) => {
            event.preventDefault();
            void submit();
          }}
        >
          <fieldset className="flex flex-wrap gap-4 text-sm text-ink">
            <legend className="sr-only">Import method</legend>
            <label className="flex items-center gap-2">
              <input type="radio" name="import-mode" checked={mode === "paste"} onChange={() => setMode("paste")} />
              Paste a posting
            </label>
            <label className="flex items-center gap-2">
              <input type="radio" name="import-mode" checked={mode === "file"} onChange={() => setMode("file")} />
              Upload CSV or JSON
            </label>
          </fieldset>

          {enabled.length > 1 && (
            <label className="block text-sm text-ink">
              Source
              <select className={`${FIELD} mt-1`} value={slug} onChange={(e) => setSourceSlug(e.target.value)}>
                {enabled.map((s) => (
                  <option key={s.id} value={s.slug}>
                    {s.name}
                  </option>
                ))}
              </select>
            </label>
          )}

          {mode === "paste" ? (
            <div className="grid gap-3 md:grid-cols-2">
              <label className="block text-sm text-ink">
                Job title
                <input className={`${FIELD} mt-1`} value={posting.title} onChange={(e) => set("title")(e.target.value)} maxLength={300} />
              </label>
              <label className="block text-sm text-ink">
                Company
                <input className={`${FIELD} mt-1`} value={posting.company} onChange={(e) => set("company")(e.target.value)} maxLength={300} />
              </label>
              <label className="block text-sm text-ink">
                Posting link
                <input className={`${FIELD} mt-1`} type="url" inputMode="url" value={posting.url} onChange={(e) => set("url")(e.target.value)} placeholder="https://" />
              </label>
              <label className="block text-sm text-ink">
                Location (optional)
                <input className={`${FIELD} mt-1`} value={posting.location} onChange={(e) => set("location")(e.target.value)} maxLength={300} />
              </label>
              <label className="block text-sm text-ink md:col-span-2">
                Job description
                <textarea className={`${FIELD} mt-1 min-h-48`} value={posting.description} onChange={(e) => set("description")(e.target.value)} />
              </label>
            </div>
          ) : (
            <label className="block text-sm text-ink">
              File (.csv or .json, up to 5 MB)
              <input
                className="mt-1 block text-sm text-ink"
                type="file"
                accept={ACCEPTED.join(",")}
                onChange={(e) => setFile(e.target.files?.[0] ?? null)}
              />
            </label>
          )}

          {problems.length > 0 && (
            <ul role="alert" className="list-disc space-y-1 pl-5 text-sm text-danger">
              {problems.map((p) => (
                <li key={p}>{p}</li>
              ))}
            </ul>
          )}

          <button type="submit" disabled={busy} className={`${BUTTON} bg-inset text-attention`}>
            {busy ? "Importing…" : "Import"}
          </button>
        </form>
      )}

      {result && (
        <div role="status" className="mt-4 rounded-card border border-edge p-3 text-sm text-ink">
          <p>
            {result.total} processed: {result.created} added, {result.updated} updated, {result.duplicates} duplicate,{" "}
            {result.rejected} rejected.
          </p>
          <ul className="mt-2 space-y-1">
            {result.outcomes.map((o, index) => (
              <li key={`${o.url}-${index}`} className="break-all">
                <span className="font-medium">{o.result}</span>: {o.url}
                {o.reason ? ` — ${o.reason}` : ""}
                {o.job_id && (o.result === "created" || o.result === "updated") ? (
                  <>
                    {" "}
                    <Link className="text-accent underline" href={`/job-feed/${o.job_id}`}>
                      Open job
                    </Link>
                  </>
                ) : null}
              </li>
            ))}
          </ul>
          <p className="mt-2 text-ink-soft">
            Requirements are extracted during import without any AI call. The fit score appears once the
            background evaluator has processed the job.
          </p>
        </div>
      )}
    </section>
  );
}
