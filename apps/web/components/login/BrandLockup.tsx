/**
 * The JobForge lockup for the sign-in page.
 *
 * The mark is the same three-plane anvil face as `Wordmark` in
 * components/Icon.tsx - the paths are duplicated rather than imported because
 * this lockup needs a size scale and a tagline that the compact sidebar
 * wordmark does not have. If the mark ever changes, both must change.
 *
 * Decorative: the accessible name of the page comes from the heading in the
 * card, not from here.
 */

type Size = "corner" | "card";

const MARK: Record<Size, string> = {
  corner: "h-9 w-9 sm:h-11 sm:w-11",
  card: "h-7 w-7",
};

const NAME: Record<Size, string> = {
  corner: "text-[1.75rem] sm:text-[2.25rem]",
  card: "text-[1.3125rem]",
};

const TAGLINE: Record<Size, string> = {
  corner: "text-[0.625rem] sm:text-[0.6875rem] tracking-[0.42em]",
  card: "text-[0.625rem] tracking-[0.34em]",
};

export function BrandLockup({ size = "card" }: { size?: Size }) {
  return (
    <span className="inline-flex flex-col items-center">
      <span className="flex items-center gap-2.5">
        <svg viewBox="0 0 24 24" className={MARK[size]} aria-hidden="true" focusable="false">
          <path d="M3 8h13l-2.2 4.2H8.8L11 8" className="fill-accent" />
          <path d="M8.8 12.2h5L12 17H6.4l2.4-4.8Z" className="fill-ink" />
          <path d="M15.4 12.2H21L18.6 17H13l2.4-4.8Z" className="fill-action" />
        </svg>
        <span className={`font-semibold tracking-tight text-ink ${NAME[size]}`}>JobForge</span>
      </span>
      <span
        className={`mt-1 font-semibold uppercase text-ink-soft ${TAGLINE[size]}`}
      >
        Build Apply Grow
      </span>
    </span>
  );
}
