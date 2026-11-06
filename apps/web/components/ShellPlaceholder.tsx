"use client";

/**
 * What the first session check shows: the shell's own frame on the cream
 * canvas (sidebar column and header bar), never a blank page or a bare line
 * of text. Later navigations never show it; the guard re-checks quietly.
 */
export function ShellPlaceholder() {
  return (
    <div className="min-h-screen lg:grid lg:grid-cols-[15rem_1fr]" data-testid="auth-loading">
      <div aria-hidden="true" className="hidden border-r border-edge bg-surface lg:block" />
      <div className="min-w-0">
        <div aria-hidden="true" className="h-[76px] border-b border-edge bg-canvas" />
        <p role="status" className="px-5 py-7 text-sm text-ink-soft lg:px-9 lg:py-9">Checking your session…</p>
      </div>
    </div>
  );
}
