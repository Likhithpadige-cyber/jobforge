/**
 * The shared shell for a sign-in field: visible label, leading glyph, and the
 * pill input the reference artwork calls for.
 *
 * The label is visible rather than a placeholder. The reference shows
 * placeholder-only fields, but a placeholder disappears the moment you type,
 * which is exactly when a three-field form needs its labels most. The pill
 * shape, the glyph and the spacing carry the reference's look; the label
 * carries the meaning.
 */

import type { ReactNode } from "react";

export const FIELD_INPUT_CLASS =
  "w-full rounded-[0.875rem] border border-white/70 bg-white/80 py-2.5 pl-11 pr-3 text-sm text-ink " +
  "shadow-[inset_0_1px_2px_rgba(36,40,59,0.05)] outline-none transition " +
  "placeholder:text-ink-soft/70 hover:bg-white/90 focus:border-violet/45 focus:bg-white";

export function LoginField({
  htmlFor,
  label,
  glyph,
  children,
}: {
  htmlFor: string;
  label: string;
  glyph: ReactNode;
  children: ReactNode;
}) {
  return (
    <div>
      <label htmlFor={htmlFor} className="block text-xs font-semibold text-ink">
        {label}
      </label>
      <div className="relative mt-1">
        <span
          aria-hidden="true"
          className="pointer-events-none absolute left-3.5 top-1/2 -translate-y-1/2 text-ink-soft"
        >
          {glyph}
        </span>
        {children}
      </div>
    </div>
  );
}

const GLYPH_PROPS = {
  viewBox: "0 0 24 24",
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 1.5,
  strokeLinecap: "round" as const,
  strokeLinejoin: "round" as const,
  className: "h-[18px] w-[18px]",
  "aria-hidden": true,
  focusable: "false" as const,
};

export const MailGlyph = () => (
  <svg {...GLYPH_PROPS}>
    <path d="M3.5 6.5h17v11h-17v-11Zm0 .5 8.5 6 8.5-6" />
  </svg>
);

export const LockGlyph = () => (
  <svg {...GLYPH_PROPS}>
    <path d="M6.5 10.5h11v9h-11v-9Zm2.25 0V8a3.25 3.25 0 0 1 6.5 0v2.5" />
  </svg>
);

export const CodeGlyph = () => (
  <svg {...GLYPH_PROPS}>
    <path d="M12 3.5 19 6.5v5.2c0 4.1-2.9 7.8-7 8.8-4.1-1-7-4.7-7-8.8V6.5l7-3Zm-2.5 8.5 1.9 1.9 3.4-3.6" />
  </svg>
);
