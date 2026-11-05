import { Icon } from "./Icon";

/**
 * Marks a section the backend cannot fill yet.
 *
 * The product's argument is that you can always see where a number came
 * from. Leaving a blank panel, or worse filling it with a plausible number,
 * would undo that on the screen where trust matters most. So the gap is
 * stated, with the contract that would close it.
 */
export function NotWiredNotice({
  title,
  body,
  needs,
}: {
  title: string;
  body: string;
  needs?: string;
}) {
  return (
    <div className="rounded-card border border-dashed border-edge bg-inset p-4">
      <div className="flex items-start gap-3">
        <Icon name="activity" className="mt-0.5 h-4 w-4 shrink-0 text-ink-soft" />
        <div className="min-w-0">
          <p className="text-sm font-medium text-ink">{title}</p>
          <p className="mt-1 text-sm leading-relaxed text-ink-soft">{body}</p>
          {needs && (
            <p className="mt-2 font-mono text-xs text-ink-soft">Needs: {needs}</p>
          )}
        </div>
      </div>
    </div>
  );
}
