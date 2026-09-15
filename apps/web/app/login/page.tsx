"use client";

/**
 * Sign in.
 *
 * Both factors are submitted together, never in two steps: a "password
 * accepted, now enter your code" flow would reveal whether the password alone
 * was correct.
 *
 * Whether the authenticator code is required comes from `GET /auth/config`,
 * not from a build-time flag, so the form matches the deployment it is
 * actually talking to. The server decides regardless of what this form sends.
 *
 * The session cookie is HttpOnly, so this page never sees or stores it. On
 * success the browser holds the cookie and the app bootstraps from
 * `GET /auth/me`.
 */

import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";
import { ApiError, api } from "@/lib/api";

function LoginForm() {
  const router = useRouter();
  const params = useSearchParams();
  const next = params.get("next") || "/";

  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [totp, setTotp] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  // Assume MFA is required until told otherwise: if the probe fails, the
  // stricter form is the safe default.
  const [totpRequired, setTotpRequired] = useState(true);

  useEffect(() => {
    let cancelled = false;
    api
      .authConfig()
      .then((cfg) => {
        if (!cancelled) setTotpRequired(cfg.totp_required);
      })
      .catch(() => {
        /* keep the strict default */
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const canSubmit =
    email.trim() !== "" &&
    password !== "" &&
    (!totpRequired || totp.trim() !== "") &&
    !submitting;

  async function onSubmit(event: React.FormEvent) {
    event.preventDefault();
    if (!canSubmit) return;
    setSubmitting(true);
    setError(null);
    try {
      const code = totp.trim();
      await api.login({
        email: email.trim(),
        password,
        // Omitted entirely when blank, rather than sent as "".
        ...(code ? { totp_code: code } : {}),
      });
      setPassword("");
      setTotp("");
      router.replace(next);
    } catch (cause) {
      // The API answers every bad-credential case identically; say the same.
      setError(
        cause instanceof ApiError && cause.status === 401
          ? "Invalid email, password or code."
          : cause instanceof Error
            ? cause.message
            : "Could not sign in.",
      );
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <main className="mx-auto flex min-h-screen max-w-sm flex-col justify-center px-4">
      <div className="rounded-card border border-edge bg-surface p-6">
        <h1 className="text-lg font-semibold text-ink">Sign in</h1>
        <p className="mt-1 text-sm text-ink-soft">
          {totpRequired
            ? "JobForge requires your password and an authenticator code."
            : "Local development: your password is enough."}
        </p>

        <form onSubmit={onSubmit} className="mt-5 space-y-4" noValidate>
          <div>
            <label htmlFor="email" className="block text-sm font-medium text-ink">
              Email
            </label>
            <input
              id="email"
              name="email"
              type="email"
              autoComplete="username"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              className="mt-1 w-full rounded-chip border border-edge bg-surface px-3 py-2 text-sm text-ink"
            />
          </div>

          <div>
            <label htmlFor="password" className="block text-sm font-medium text-ink">
              Password
            </label>
            <input
              id="password"
              name="password"
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="mt-1 w-full rounded-chip border border-edge bg-surface px-3 py-2 text-sm text-ink"
            />
          </div>

          {totpRequired && (
          <div>
            <label htmlFor="totp" className="block text-sm font-medium text-ink">
              Authenticator code
            </label>
            <input
              id="totp"
              name="totp"
              type="text"
              inputMode="numeric"
              autoComplete="one-time-code"
              maxLength={10}
              value={totp}
              onChange={(e) => setTotp(e.target.value)}
              className="mt-1 w-full rounded-chip border border-edge bg-surface px-3 py-2 text-sm tabular-nums text-ink"
            />
          </div>
          )}

          {error && (
            <p
              role="alert"
              className="rounded-card border border-signal-risk/25 bg-signal-risk/[0.06] px-3 py-2 text-sm text-signal-risk"
            >
              {error}
            </p>
          )}

          <button
            type="submit"
            disabled={!canSubmit}
            className="w-full rounded-chip border border-brass bg-brass-soft px-3 py-2 text-sm font-medium text-brass-deep disabled:cursor-not-allowed disabled:opacity-60"
          >
            {submitting ? "Signing in…" : "Sign in"}
          </button>
        </form>
      </div>
    </main>
  );
}

export default function Page() {
  return (
    <Suspense fallback={null}>
      <LoginForm />
    </Suspense>
  );
}
