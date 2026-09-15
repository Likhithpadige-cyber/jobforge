"use client";

import { Icon } from "./Icon";

/**
 * Workspace header.
 *
 * Deliberately thin: title, one line of orientation, search, and two
 * controls. Anything else competes with the content below it.
 */
export function Header({
  title,
  description,
  onOpenNav,
}: {
  title: string;
  description?: string;
  onOpenNav?: () => void;
}) {
  return (
    <header className="sticky top-0 z-20 border-b border-edge bg-canvas/85 backdrop-blur">
      <div className="flex flex-wrap items-center gap-4 px-5 py-4 lg:px-8">
        <button
          type="button"
          onClick={onOpenNav}
          className="rounded-chip border border-edge bg-surface p-2 text-ink-soft hover:text-ink lg:hidden"
          aria-label="Open navigation"
        >
          <Icon name="feed" className="h-4 w-4" />
        </button>

        <div className="min-w-0 flex-1">
          <h1 className="truncate text-display font-semibold text-ink">{title}</h1>
          {description && (
            <p className="mt-0.5 truncate text-sm text-ink-soft">{description}</p>
          )}
        </div>

        <div className="order-last w-full sm:order-none sm:w-auto">
          <label htmlFor="job-search" className="sr-only">
            Search jobs by role, company or location
          </label>
          <div className="relative">
            <Icon
              name="search"
              className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-ink-soft"
            />
            <input
              id="job-search"
              type="search"
              placeholder="Search roles, companies, locations"
              className="h-9 w-full rounded-chip border border-edge bg-surface pl-9 pr-3 text-sm text-ink placeholder:text-ink-soft/70 sm:w-72"
            />
          </div>
        </div>

        <button
          type="button"
          className="relative rounded-chip border border-edge bg-surface p-2 text-ink-soft hover:text-ink"
          aria-label="Notifications, 4 items need your attention"
        >
          <Icon name="bell" className="h-4 w-4" />
          <span
            className="absolute right-1.5 top-1.5 h-1.5 w-1.5 rounded-full bg-brass"
            aria-hidden="true"
          />
        </button>
      </div>
    </header>
  );
}
