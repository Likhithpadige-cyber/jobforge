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
  // The corner lockup sits on the sky, where ink-soft would fall below a
  // readable contrast ratio; a light halo keeps it legible over both the
  // blue upper sky and the bright water without changing the brand colour.
  const onSky = size === "corner";
  const halo = onSky ? "[text-shadow:0_1px_10px_rgba(255,255,255,0.75)]" : "";

  return (
    <span className={`inline-flex flex-col items-center ${halo}`}>
      <span className="flex items-center gap-2.5">
        <svg viewBox="0 0 24 24" className={MARK[size]} aria-hidden="true" focusable="false">
          <path d="M3 8h13l-2.2 4.2H8.8L11 8" fill="#6457B5" />
          <path d="M8.8 12.2h5L12 17H6.4l2.4-4.8Z" fill="#4D4291" />
          <path d="M15.4 12.2H21L18.6 17H13l2.4-4.8Z" fill="#C9AE78" />
        </svg>
        <span className={`font-semibold tracking-tight text-ink ${NAME[size]}`}>JobForge</span>
      </span>
      <span
        className={`mt-1 font-semibold uppercase ${onSky ? "text-ink/70" : "text-ink-soft"} ${TAGLINE[size]}`}
      >
        Build Apply Grow
      </span>
    </span>
  );
}
