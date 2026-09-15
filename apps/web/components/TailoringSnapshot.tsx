/**
 * Tailoring Snapshot.
 *
 * Deliberately NOT called an ATS score. No scoring model exists yet, so this
 * panel reports only counts that come straight from persisted verdicts. An
 * invented number here would be exactly the kind of unverified claim the whole
 * pipeline exists to prevent.
 */

import type { ApiResume } from "@/lib/api";
import { VERIFICATION_LABEL, applicationLabel } from "@/lib/resumes";

function Stat({
  label,
  value,
  tone = "",
}: {
  label: string;
  value: string | number;
  tone?: string;
}) {
  return (
    <div className="flex items-baseline justify-between gap-4 py-2">
      <dt className="text-sm text-ink-soft">{label}</dt>
      <dd className={`text-sm font-medium tabular-nums ${tone || "text-ink"}`}>{value}</dd>
    </div>
  );
}

export function TailoringSnapshot({ resume }: { resume: ApiResume }) {
  const { counts } = resume;
  return (
    <section
      aria-labelledby="tailoring-snapshot"
      className="rounded-card border border-edge bg-surface p-5"
    >
      <h2 id="tailoring-snapshot" className="text-sm font-semibold text-ink">
        Tailoring Snapshot
      </h2>
      <p className="mt-1 text-xs text-ink-soft">
        Verification facts only — not an ATS score.
      </p>
      <dl className="mt-3 divide-y divide-edge">
        <Stat label="Supported claims" value={counts.supported} tone="text-signal-strong" />
        <Stat
          label="Overreach"
          value={counts.overreach}
          tone={counts.overreach > 0 ? "text-signal-review" : undefined}
        />
        <Stat
          label="Unsupported"
          value={counts.unsupported}
          tone={counts.unsupported > 0 ? "text-signal-risk" : undefined}
        />
        <Stat label="Total claims" value={counts.total} />
        <Stat label="Resume version" value={`v${resume.version}`} />
        <Stat label="Verification" value={VERIFICATION_LABEL[resume.verification_status]} />
        <Stat label="Application" value={applicationLabel(resume.application_status)} />
      </dl>
    </section>
  );
}
