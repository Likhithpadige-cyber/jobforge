"use client";

/**
 * Owner-only master resume import from plain text.
 *
 * The text is split deterministically into sections by recognised headings
 * and into entries by lines/bullets, shown as an editable preview, and saved
 * only when the owner confirms. Saving stores the reviewed lines through
 * POST /profiles/{id}/master-resumes, which never verifies anything: these
 * lines are not evidence until individual facts are verified in Profile &
 * Facts. PDF/DOCX text extraction is not offered here.
 */

import { useState } from "react";
import { apiErrorText } from "@/lib/api";
import { profile as profileApi, type MasterSectionName } from "@/lib/profile";

export const MAX_MASTER_BYTES = 200_000;
export const SECTION_NAMES: MasterSectionName[] = [
  "summary",
  "experience",
  "skills",
  "projects",
  "education",
  "certifications",
];
const LABEL: Record<MasterSectionName, string> = {
  summary: "Summary",
  experience: "Experience",
  skills: "Skills",
  projects: "Projects",
  education: "Education",
  certifications: "Certifications",
};

const HEADINGS: [RegExp, MasterSectionName][] = [
  [/^(professional\s+)?(summary|profile|objective|about( me)?)$/, "summary"],
  [/^(professional\s+|work\s+|employment\s+)?(experience|history|employment)$/, "experience"],
  [/^(technical\s+|core\s+|key\s+)?(skills|competencies|technologies|tools)$/, "skills"],
  [/^(selected\s+|personal\s+)?projects$/, "projects"],
  [/^education(\s+and\s+training)?$/, "education"],
  [/^(certifications?|licen[cs]es?|certifications\s+and\s+licen[cs]es)$/, "certifications"],
];

export interface PreviewLine {
  key: number;
  section: MasterSectionName;
  text: string;
}

function headingOf(line: string): MasterSectionName | null {
  const cleaned = line.replace(/^#+\s*/, "").replace(/[:\s]+$/, "").trim().toLowerCase();
  if (!cleaned || cleaned.length > 60) return null;
  for (const [pattern, name] of HEADINGS) if (pattern.test(cleaned)) return name;
  return null;
}

/** Deterministic split of pasted text into section lines. Unrecognised text before any heading becomes summary. */
export function parseMasterText(text: string): PreviewLine[] {
  const lines: PreviewLine[] = [];
  let section: MasterSectionName = "summary";
  let key = 0;
  for (const raw of text.replace(/\r\n?/g, "\n").split("\n")) {
    const line = raw.trim();
    if (!line) continue;
    const heading = headingOf(line);
    if (heading) {
      section = heading;
      continue;
    }
    const entry = line.replace(/^([-*•·▪‣]|\d+[.)])\s+/, "").trim();
    if (entry) lines.push({ key: key++, section, text: entry.slice(0, 2000) });
  }
  return lines;
}

/** The MasterResumeData body; section order follows SECTION_NAMES. */
export function toMasterData(lines: PreviewLine[], sourceLabel: string) {
  const sections = SECTION_NAMES.map((name) => ({
    name,
    entries: lines.filter((l) => l.section === name && l.text.trim()).map((l) => ({ text: l.text.trim() })),
  })).filter((s) => s.entries.length > 0);
  return { schema_version: 1 as const, source_label: sourceLabel.trim().slice(0, 120) || "Pasted text", sections };
}

export function MasterResumeImport({ profileId, onSaved }: { profileId: string; onSaved: (message: string) => void }) {
  const [open, setOpen] = useState(false);
  const [text, setText] = useState("");
  const [label, setLabel] = useState("");
  const [preview, setPreview] = useState<PreviewLine[] | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function readFile(file: File | undefined) {
    setProblem(null);
    if (!file) return;
    if (!/\.(txt|md)$/i.test(file.name)) return setProblem("Only .txt or .md files can be read here. Copy the text of a PDF or DOCX and paste it instead.");
    if (file.size > MAX_MASTER_BYTES) return setProblem("The file is larger than 200 KB.");
    setText(await file.text());
    setLabel(file.name);
  }

  function buildPreview() {
    const lines = parseMasterText(text);
    if (lines.length === 0) return setProblem("No resume lines were found. Paste the resume text first.");
    setProblem(null);
    setPreview(lines);
  }

  const update = (key: number, change: Partial<PreviewLine>) =>
    setPreview((current) => current?.map((l) => (l.key === key ? { ...l, ...change } : l)) ?? null);

  async function save() {
    if (!preview) return;
    const data = toMasterData(preview, label);
    if (data.sections.length === 0) return setProblem("Nothing is left to save.");
    if (data.sections.some((s) => s.entries.length > 100)) return setProblem("A section has more than 100 lines; remove some first.");
    setBusy(true);
    setProblem(null);
    try {
      const result = await profileApi.importMasterResume(profileId, data);
      onSaved(
        `Master resume version ${result.version} saved. These lines are not evidence: verify individual facts below before any resume can cite them.`,
      );
      setOpen(false);
      setPreview(null);
      setText("");
    } catch (cause) {
      setProblem(apiErrorText(cause, "The master resume could not be saved."));
    } finally {
      setBusy(false);
    }
  }

  const BUTTON = "inline-flex min-h-11 items-center rounded-chip border border-edge px-4 py-2 text-sm font-semibold disabled:opacity-60";

  return (
    <section aria-labelledby="master-import-heading" className="rounded-card border border-edge bg-surface p-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 id="master-import-heading" className="text-base font-semibold text-ink">Master resume</h2>
          <p className="mt-1 text-sm text-ink-soft">Paste your resume text or open a .txt/.md file, review the lines, then save.</p>
        </div>
        <button type="button" aria-expanded={open} onClick={() => setOpen((v) => !v)} className={`${BUTTON} bg-surface text-ink`}>
          {open ? "Close" : "Import master resume"}
        </button>
      </div>
      {open && (
        <div className="mt-4 space-y-3">
          <label className="block text-sm text-ink">
            Resume text
            <textarea className="mt-1 min-h-48 w-full rounded-chip border border-edge bg-surface px-3 py-2 text-sm text-ink" value={text} onChange={(e) => setText(e.target.value)} />
          </label>
          <label className="block text-sm text-ink">
            Or open a text file (.txt or .md, up to 200 KB)
            <input className="mt-1 block text-sm" type="file" accept=".txt,.md,text/plain,text/markdown" onChange={(e) => void readFile(e.target.files?.[0])} />
          </label>
          <button type="button" onClick={buildPreview} className={`${BUTTON} bg-surface text-ink`}>Preview lines</button>
          {preview && (
            <div aria-label="Master resume preview" className="space-y-2">
              <p className="text-xs text-ink-soft">Check each line&apos;s section and wording. Nothing here is verified.</p>
              <ul className="space-y-2">
                {preview.map((line) => (
                  <li key={line.key} className="grid gap-2 sm:grid-cols-[10rem_1fr_auto]">
                    <select aria-label={`Section for line ${line.key + 1}`} className="rounded-chip border border-edge bg-surface px-2 text-sm" value={line.section} onChange={(e) => update(line.key, { section: e.target.value as MasterSectionName })}>
                      {SECTION_NAMES.map((n) => <option key={n} value={n}>{LABEL[n]}</option>)}
                    </select>
                    <input aria-label={`Text of line ${line.key + 1}`} className="rounded-chip border border-edge bg-surface px-2 py-1 text-sm" value={line.text} maxLength={2000} onChange={(e) => update(line.key, { text: e.target.value })} />
                    <button type="button" aria-label={`Remove line ${line.key + 1}`} className="rounded-chip border border-edge px-2 text-sm" onClick={() => setPreview((c) => c?.filter((l) => l.key !== line.key) ?? null)}>Remove</button>
                  </li>
                ))}
              </ul>
              <button type="button" disabled={busy} onClick={() => void save()} className={`${BUTTON} bg-inset text-attention`}>
                {busy ? "Saving…" : "Save master resume"}
              </button>
            </div>
          )}
          {problem && <p role="alert" className="text-sm text-danger">{problem}</p>}
        </div>
      )}
    </section>
  );
}
