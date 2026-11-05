"use client";

/** Generic boundary UI never exposes exception messages, requests or user data. */
export function ErrorFallback({ reset }: { reset: () => void }) {
  return <main id="content" className="flex min-h-screen items-center justify-center bg-canvas p-6">
    <section role="alert" className="w-full max-w-lg rounded-card border border-danger bg-surface p-8">
      <p className="text-xs font-semibold uppercase tracking-widest text-accent">JobForge</p>
      <h1 className="mt-4 text-2xl font-semibold text-ink">This page could not be loaded</h1>
      <p className="mt-3 text-sm leading-6 text-ink-soft">Try loading it again. If the problem continues, return later.</p>
      <button type="button" onClick={reset} className="mt-6 min-h-11 rounded-chip bg-action px-4 text-sm font-semibold text-action-ink">Try again</button>
    </section>
  </main>;
}
