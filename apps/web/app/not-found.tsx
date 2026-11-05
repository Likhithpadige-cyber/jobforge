import Link from "next/link";

export default function NotFound() {
  return <main id="content" className="flex min-h-screen items-center justify-center bg-canvas p-6">
    <section className="w-full max-w-lg rounded-card border border-edge bg-surface p-8">
      <p className="text-xs font-semibold uppercase tracking-widest text-accent">JobForge · 404</p>
      <h1 className="mt-4 text-2xl font-semibold text-ink">This page could not be found</h1>
      <p className="mt-3 text-sm leading-6 text-ink-soft">The address may have changed. Return to your workspace to continue.</p>
      <Link href="/" className="mt-6 inline-flex min-h-11 items-center rounded-chip bg-action px-4 text-sm font-semibold text-action-ink">Back to workspace</Link>
    </section>
  </main>;
}
