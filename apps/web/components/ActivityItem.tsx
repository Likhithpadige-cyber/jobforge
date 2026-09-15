import { ProvenanceTag } from "./ProvenanceTag";
import type { DemoActivity } from "@/lib/demo-data";

/**
 * One thing JobForge did.
 *
 * Laid out so the whole row can become a link in a later phase without
 * moving anything: the hover affordance and padding already sit on the list
 * item. No drill-down is wired up yet, so it stays a plain <li> rather than
 * a button that goes nowhere.
 */
export function ActivityItem({ item }: { item: DemoActivity }) {
  return (
    <li className="group -mx-2 flex gap-3.5 rounded-chip px-2 py-3.5 transition-colors">
      <span
        className="mt-2 h-1.5 w-1.5 shrink-0 rounded-full bg-edge"
        aria-hidden="true"
      />
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1">
          <p className="text-sm font-medium leading-snug text-ink">{item.action}</p>
          <ProvenanceTag kind={item.provenance} />
          <span className="ml-auto shrink-0 text-xs text-ink-soft">{item.at}</span>
        </div>
        <p className="mt-1 text-sm leading-relaxed text-ink-soft">{item.detail}</p>
      </div>
    </li>
  );
}
