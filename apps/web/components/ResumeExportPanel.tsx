"use client";

/**
 * PDF/DOCX download for the current resume version.
 *
 * The browser only asks for files the API already rendered. Every download
 * goes back through GET /artifacts/{id}/content, where the server re-checks
 * ownership, export consent, current evidence and the stored checksum; this
 * panel cannot widen any of that. After download the bytes are hashed here
 * and compared with the listed SHA-256, so a truncated or substituted file is
 * reported instead of saved.
 */

import { useCallback, useEffect, useState } from "react";
import { api, apiErrorText, downloadArtifactBytes, type ApiArtifact } from "@/lib/api";

const KINDS = [
  { kind: "resume_pdf", label: "PDF", ext: "pdf" },
  { kind: "resume_docx", label: "DOCX", ext: "docx" },
] as const;

const BUTTON =
  "inline-flex min-h-11 w-full items-center justify-center rounded-chip border border-edge px-3 py-2 text-sm font-semibold disabled:cursor-not-allowed disabled:opacity-60";

/** Contact-bearing templates first; the contact-free legacy text template second. */
export function pickCurrent(artifacts: ApiArtifact[], version: number, kind: string): ApiArtifact | null {
  const candidates = artifacts
    .filter((a) => a.kind === kind && a.resume_version === version)
    .sort((a, b) => {
      const rank = (t: string | null) => (t?.startsWith("ats-single-column/2") ? 0 : t?.includes("legacy-text") ? 1 : 2);
      return rank(a.template_version) - rank(b.template_version) || b.created_at.localeCompare(a.created_at);
    });
  return candidates[0] ?? null;
}

export async function sha256Hex(bytes: ArrayBuffer): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, "0")).join("");
}

/** A conservative local filename; the server's own name is preferred. */
export function safeFilename(name: string | null, version: number, ext: string): string {
  const fallback = `jobforge-resume-v${version}.${ext}`;
  if (!name) return fallback;
  const cleaned = name.replace(/[^A-Za-z0-9._-]/g, "_").replace(/^\.+/, "");
  return cleaned.toLowerCase().endsWith(`.${ext}`) ? cleaned.slice(0, 120) : fallback;
}

function save(bytes: ArrayBuffer, filename: string, type: string) {
  const url = URL.createObjectURL(new Blob([bytes], { type }));
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export function ResumeExportPanel({ resumeId, version }: { resumeId: string; version: number }) {
  const [artifacts, setArtifacts] = useState<ApiArtifact[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [message, setMessage] = useState<{ tone: "ok" | "error"; text: string } | null>(null);

  const load = useCallback(async () => {
    setLoadError(null);
    try {
      setArtifacts(await api.listResumeArtifacts(resumeId));
    } catch (cause) {
      setLoadError(apiErrorText(cause, "Export status is unavailable."));
    }
  }, [resumeId]);

  useEffect(() => {
    void load();
  }, [load]);

  async function download(artifact: ApiArtifact, ext: string, label: string) {
    setBusy(artifact.id);
    setMessage(null);
    try {
      const file = await downloadArtifactBytes(artifact.id);
      if (!artifact.sha256 || (await sha256Hex(file.bytes)) !== artifact.sha256) {
        setMessage({ tone: "error", text: `The ${label} did not match its recorded checksum and was not saved.` });
        return;
      }
      save(file.bytes, safeFilename(file.filename, version, ext), file.contentType);
      setMessage({ tone: "ok", text: `${label} downloaded and its checksum verified.` });
    } catch (cause) {
      setMessage({ tone: "error", text: apiErrorText(cause, `The ${label} could not be downloaded.`) });
    } finally {
      setBusy(null);
    }
  }

  const rendered = artifacts ? KINDS.map((k) => ({ ...k, artifact: pickCurrent(artifacts, version, k.kind) })) : [];
  const nothing = artifacts !== null && rendered.every((r) => !r.artifact);

  return (
    <section aria-labelledby="export-heading" className="rounded-card border border-edge bg-surface p-5">
      <h2 id="export-heading" className="text-sm font-semibold text-ink">
        Export
      </h2>
      {loadError && (
        <p role="alert" className="mt-2 text-xs text-danger">
          {loadError}
        </p>
      )}
      {artifacts === null && !loadError && <p className="mt-2 text-xs text-ink-soft">Checking export status…</p>}
      {nothing && (
        <p className="mt-2 text-xs leading-5 text-ink-soft">
          No PDF or DOCX exists for version {version} yet. Files are rendered after the resume is approved and the
          owner has approved the exact contact and career details for export.
        </p>
      )}
      {!nothing && artifacts && (
        <div className="mt-3 space-y-2">
          {rendered.map(({ kind, label, ext, artifact }) =>
            artifact ? (
              <button
                key={kind}
                type="button"
                disabled={busy !== null}
                onClick={() => void download(artifact, ext, label)}
                className={`${BUTTON} bg-inset text-attention`}
              >
                {busy === artifact.id ? `Downloading ${label}…` : `Download ${label}`}
              </button>
            ) : (
              <p key={kind} className="text-xs text-ink-soft">
                {label} not rendered for version {version}.
              </p>
            ),
          )}
          {rendered.some((r) => r.artifact?.template_version?.includes("legacy-text")) && (
            <p className="text-xs text-ink-soft">
              This file uses the text-only template and contains no contact details. Approve the export details to
              produce a complete resume.
            </p>
          )}
        </div>
      )}
      {message && (
        <p role={message.tone === "error" ? "alert" : "status"} className={`mt-3 text-xs ${message.tone === "error" ? "text-danger" : "text-success"}`}>
          {message.text}
        </p>
      )}
    </section>
  );
}
