/**
 * Inline icon set.
 *
 * Hand-drawn rather than a dependency: the project has no icon library and
 * this increment needs eight glyphs. All are 1.5px stroke on a 24 grid so
 * they sit consistently, and all are decorative (aria-hidden) - meaning is
 * always carried by adjacent text.
 */

type IconProps = {
  name: IconName;
  className?: string;
};

export type IconName =
  | "overview"
  | "feed"
  | "approvals"
  | "resume"
  | "applications"
  | "activity"
  | "interviews"
  | "analytics"
  | "more"
  | "profile"
  | "settings"
  | "search"
  | "bell"
  | "arrow"
  | "shield"
  | "spark";

const PATHS: Record<IconName, string> = {
  overview: "M4 13h6V4H4v9Zm0 7h6v-5H4v5Zm10 0h6v-9h-6v9Zm0-16v5h6V4h-6Z",
  feed: "M4 6h16M4 12h16M4 18h10",
  approvals: "M9 12.5l2.2 2.2L15.5 10M12 3l7.5 3.3v5.2c0 4.4-3.1 8.4-7.5 9.5-4.4-1.1-7.5-5.1-7.5-9.5V6.3L12 3Z",
  resume: "M7 3h7l4 4v14H7V3Zm7 0v4h4M10 12h5M10 16h5",
  applications: "M4 7h16v13H4V7Zm4-4v4m8-4v4M4 11h16",
  activity: "M3 12h4l2.5 6 4-14 2.5 8h5",
  interviews: "M4 5h16v14H4V5Zm4 4h8m-8 4h5m-5 4h8",
  analytics: "M4 19V5m0 14h16M8 15l4-4 3 2 5-6",
  more: "M5 12h.01M12 12h.01M19 12h.01",
  profile: "M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8Zm-7 8a7 7 0 0 1 14 0",
  settings: "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6Zm8.4-3a8.4 8.4 0 0 0-.1-1.3l2-1.5-2-3.4-2.3 1a8.3 8.3 0 0 0-2.2-1.3L15.4 2h-4l-.4 2.5a8.3 8.3 0 0 0-2.2 1.3l-2.3-1-2 3.4 2 1.5a8.4 8.4 0 0 0 0 2.6l-2 1.5 2 3.4 2.3-1a8.3 8.3 0 0 0 2.2 1.3l.4 2.5h4l.4-2.5a8.3 8.3 0 0 0 2.2-1.3l2.3 1 2-3.4-2-1.5c.06-.43.1-.86.1-1.3Z",
  search: "M11 18a7 7 0 1 0 0-14 7 7 0 0 0 0 14Zm5.5-1.5L21 21",
  bell: "M18 8a6 6 0 1 0-12 0c0 6-2 7-2 7h16s-2-1-2-7ZM10.5 20a2 2 0 0 0 3 0",
  arrow: "M5 12h13m-5-5 5 5-5 5",
  shield: "M12 3l7.5 3.3v5.2c0 4.4-3.1 8.4-7.5 9.5-4.4-1.1-7.5-5.1-7.5-9.5V6.3L12 3Zm0 5v5m0 3v.5",
  spark: "M12 4v4m0 8v4m8-8h-4M8 12H4m11.7-5.7-2.8 2.8m-4 4-2.8 2.8m11.4 0-2.8-2.8m-4-4L6.9 6.3",
};

export function Icon({ name, className = "h-4 w-4" }: IconProps) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.5}
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
      aria-hidden="true"
      focusable="false"
    >
      <path d={PATHS[name]} />
    </svg>
  );
}

/**
 * The JobForge mark: a struck anvil face reduced to three planes. A forge
 * motif, but geometric - no flames, no sparks, no circuitry.
 */
export function Wordmark({ className = "" }: { className?: string }) {
  return (
    <span className={`flex items-center gap-2.5 ${className}`}>
      <svg viewBox="0 0 24 24" className="h-6 w-6" aria-hidden="true" focusable="false">
        <path d="M3 8h13l-2.2 4.2H8.8L11 8" className="fill-accent" />
        <path d="M8.8 12.2h5L12 17H6.4l2.4-4.8Z" className="fill-ink" />
        <path d="M15.4 12.2H21L18.6 17H13l2.4-4.8Z" className="fill-action" />
      </svg>
      <span className="text-[1rem] font-semibold tracking-tight text-ink">
        JobForge
      </span>
    </span>
  );
}
