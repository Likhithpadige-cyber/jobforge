/**
 * The sign-in scene.
 *
 * One image. The sunset, skyline, water, rooftop, brick pillar, character
 * and props are all baked into `jobforge-login-scene.webp`, so there is
 * nothing here to align: no measured ledge, no character offset, no layer
 * that can drift out of register with another. The seams and the patchwork
 * that the layered version could produce are gone because the layers are.
 *
 * It is a CSS `background-image`, not an <img>, and that is what makes the
 * missing-file case silent: a background that cannot load paints nothing -
 * no broken glyph, no alt text, no border, no layout shift. The gradient
 * underneath is simply what remains, which is why it is painted first and
 * always.
 *
 * `cover` + `center` holds the composition still. The artwork is 1639x959
 * (1.709:1) with the pillar centred, so on a typical desktop the crop is a
 * few pixels and the ledge sits at ~38.5% of viewport height regardless of
 * which axis binds. Narrow viewports crop inward from both sides, keeping
 * the pillar and the character, which is the right thing to lose last.
 *
 * Everything here is aria-hidden and pointer-events-none: a screen reader
 * hears the brand and the form, and nothing in this file takes focus.
 */

const ACCENTS = [
  { lines: ["Same", "Human", "Bigger", "Goals"], position: "left-10 top-[20%] xl:left-16 -rotate-3" },
  { lines: ["Better Jobs", "Brighter", "Tomorrows"], position: "right-10 top-[8%] xl:right-16 text-right" },
  { lines: ["Plan", "Apply", "Improve", "Repeat"], position: "right-10 top-[30%] xl:right-16 text-right rotate-2" },
  { lines: ["Discipline", "Creates", "Opportunities"], position: "bottom-10 left-10 xl:left-16" },
];

export function LoginScene() {
  return (
    <div aria-hidden="true" className="pointer-events-none absolute inset-0 overflow-hidden">
      {/* Painted first and always: this is the fallback if the scene file
          cannot be fetched. */}
      <div className="absolute inset-0 bg-gradient-to-b from-[#8FB6E6] via-[#EFC6A2] to-[#F6DCC0]" />

      <div className="absolute inset-0 bg-[url('/login/jobforge-login-scene.webp')] bg-cover bg-center" />

      {ACCENTS.map((accent) => (
        <p
          key={accent.lines.join("")}
          className={`absolute hidden font-serif text-[1.05rem] italic leading-[1.35] text-violet-deep [text-shadow:0_1px_10px_rgba(255,255,255,0.7)] lg:block ${accent.position}`}
        >
          {accent.lines.map((line) => (
            <span key={line} className="block">
              {line}
            </span>
          ))}
        </p>
      ))}
    </div>
  );
}
