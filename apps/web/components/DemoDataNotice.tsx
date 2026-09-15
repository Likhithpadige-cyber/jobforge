/**
 * States plainly that the numbers below are placeholders.
 *
 * The product's whole argument is that you can see where a number came from.
 * Showing invented figures without saying so would undercut that on the
 * first screen a person sees.
 */
export function DemoDataNotice() {
  return (
    <p className="mb-5 rounded-card border border-edge bg-muted px-3 py-2 text-xs text-ink-soft">
      Sample data. The evaluation backend is live, but job feed and activity
      endpoints are not built yet — these figures are placeholders from{" "}
      <code className="font-mono text-[0.7rem]">lib/demo-data.ts</code>.
    </p>
  );
}
