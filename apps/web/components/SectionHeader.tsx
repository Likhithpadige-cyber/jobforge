import type { ReactNode } from "react";

/**
 * Section heading with an optional action on the right.
 *
 * `description` exists because most sections here need one line explaining
 * what the agent did, not because every heading needs a subtitle.
 */
export function SectionHeader({
  title,
  description,
  action,
  id,
}: {
  title: string;
  description?: string;
  action?: ReactNode;
  id?: string;
}) {
  return (
    <div className="flex flex-wrap items-end justify-between gap-4 pb-4">
      <div>
        <h2 id={id} className="text-display font-semibold text-ink">
          {title}
        </h2>
        {description && (
          <p className="mt-1.5 max-w-[68ch] text-sm leading-relaxed text-ink-soft">{description}</p>
        )}
      </div>
      {action && <div className="shrink-0">{action}</div>}
    </div>
  );
}
