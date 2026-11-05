import { Icon } from "./Icon";

/**
 * Shown when a posting contains text aimed at the evaluator.
 *
 * Calm on purpose. Nothing went wrong: the scanner did its job, the text was
 * read as data, and the evaluation carried on. Alarming the reader would
 * misrepresent what happened and make them distrust a system that worked.
 */
export function SecurityNotice({
  onInspect,
  isDemo = false,
}: {
  onInspect?: () => void;
  isDemo?: boolean;
}) {
  return (
    <section
      aria-labelledby="security-heading"
      className="relative overflow-hidden rounded-card border border-edge bg-surface"
    >
      <span className="absolute left-0 top-0 h-full w-1 bg-action" aria-hidden="true" />
      <div className="flex items-start gap-3.5 p-4 pl-5">
        <span
          className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-chip bg-inset"
          aria-hidden="true"
        >
          <Icon name="shield" className="h-4 w-4 text-danger" />
        </span>
        <div className="min-w-0">
          <p className="text-xs font-medium text-danger">Flagged</p>
          <h3 id="security-heading" className="mt-0.5 text-sm font-medium text-ink">
            This job description contains instructions aimed at JobForge
          </h3>
          <ul className="mt-2 space-y-1 text-sm leading-relaxed text-ink-soft">
            <li>The text was read as content, not followed as instructions.</li>
            <li>Nothing it asked for was added to your profile or a resume.</li>
            <li>The job was scored normally and is safe to review.</li>
          </ul>
          {onInspect && (
            <button
              type="button"
              onClick={onInspect}
              className="mt-3 inline-flex items-center gap-1 text-sm font-medium text-attention hover:underline"
            >
              See the flagged text
              <Icon name="arrow" className="h-3.5 w-3.5" />
            </button>
          )}
          {isDemo && (
            <p className="mt-3 rounded-chip bg-inset px-2 py-1 text-xs text-ink-soft">
              Sample — scan findings are not stored against jobs yet.
            </p>
          )}
        </div>
      </div>
    </section>
  );
}
