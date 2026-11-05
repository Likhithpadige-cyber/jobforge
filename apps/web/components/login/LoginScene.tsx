/** Quiet, palette-only architectural detail. No raster art or blended colors. */
export function LoginScene() {
  return (
    <div aria-hidden="true" className="pointer-events-none absolute inset-0 overflow-hidden bg-canvas">
      <div className="absolute inset-y-0 left-0 hidden w-[46%] border-r border-edge bg-surface lg:block" />
      <svg viewBox="0 0 480 400" className="absolute bottom-0 left-0 hidden w-[44%] text-edge lg:block" fill="none" stroke="currentColor" focusable="false">
        <path d="M-40 340 200 100l240 240M-40 380 200 140l240 240M0 400 200 200l200 200" />
        <path d="M80 400V260h80v140m80 0V180h80v220" />
        <path d="M160 260h80" className="stroke-accent" strokeWidth="2" />
      </svg>
    </div>
  );
}
