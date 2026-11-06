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
 *
 * The page uses the shared dark surfaces and accessible action tokens - but nothing in
 * the scene is interactive and nothing in the form is decorative. Every
 * control below posts to an endpoint that exists. There is no OAuth row, no
 * "remember me" and no "contact admin", because there is no OAuth, no
 * client-side session lifetime and no admin route to point at.
 */

import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";
import { ApiError, api } from "@/lib/api";
import { BrandLockup } from "@/components/login/BrandLockup";
import { LoginCard } from "@/components/login/LoginCard";
import { LoginScene } from "@/components/login/LoginScene";
import { PasswordField } from "@/components/login/PasswordField";
import {
  CodeGlyph,
  FIELD_INPUT_CLASS,
  LoginField,
  MailGlyph,
} from "@/components/login/LoginField";

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
    <main id="content" className="relative flex min-h-screen flex-col overflow-x-clip bg-canvas">
      <LoginScene />

      <div className="relative flex min-h-screen flex-col px-4 py-6 sm:px-8 sm:py-8">
        <div className="login-corner hidden sm:block">
          <BrandLockup size="corner" />
        </div>

        <div className="flex flex-1 items-center justify-center py-8">
          <div className="grid w-full max-w-6xl items-center gap-12 lg:grid-cols-2 lg:gap-24">
            <section className="theme-dark hidden max-w-md pr-8 lg:block" aria-label="Welcome">
              <p className="text-xs font-semibold uppercase tracking-[0.2em] text-accent">A considered next step</p>
              <h2 className="mt-5 text-4xl font-semibold leading-tight tracking-tight text-ink">Your experience.<br />Your next chapter.</h2>
              <p className="mt-5 max-w-sm text-base leading-7 text-ink-soft">A focused workspace for thoughtful applications. Review the evidence, shape your story, and keep every decision in your hands.</p>
              <div className="mt-8 flex items-center gap-3 text-xs text-ink-soft"><span className="h-0.5 w-10 bg-action" />Build. Apply. Grow.</div>
            </section>
            <div className="mx-auto w-full max-w-md">
            <LoginCard>
              <h1 className="mb-5 text-2xl font-semibold tracking-tight text-ink">Sign in to JobForge</h1>

              <div className="flex justify-center">
                <BrandLockup size="card" />
              </div>

              <p className="mt-3 text-center text-[0.8125rem] leading-5 text-ink-soft">
                {totpRequired
                  ? "JobForge requires your password and an authenticator code."
                  : "Local development: your password is enough."}
              </p>

              {/* method="post": if this form were ever submitted natively (before
                  hydration, or without JavaScript) credentials must go in a body,
                  never in a URL. React handles the real submission. */}
              <form method="post" onSubmit={onSubmit} className="mt-5 space-y-3" noValidate>
                <LoginField htmlFor="email" label="Email" glyph={<MailGlyph />}>
                  <input
                    id="email"
                    name="email"
                    type="email"
                    autoComplete="username"
                    value={email}
                    onChange={(e) => setEmail(e.target.value)}
                    className={FIELD_INPUT_CLASS}
                  />
                </LoginField>

                <PasswordField value={password} onChange={setPassword} />

                {totpRequired && (
                  <LoginField htmlFor="totp" label="Authenticator code" glyph={<CodeGlyph />}>
                    <input
                      id="totp"
                      name="totp"
                      type="text"
                      inputMode="numeric"
                      autoComplete="one-time-code"
                      maxLength={10}
                      value={totp}
                      onChange={(e) => setTotp(e.target.value)}
                      className={`${FIELD_INPUT_CLASS} tabular-nums tracking-[0.2em]`}
                    />
                  </LoginField>
                )}

                {error && (
                  <p
                    role="alert"
                    className="rounded-card border border-danger bg-inset px-3 py-2 text-sm text-danger"
                  >
                    {error}
                  </p>
                )}

                <button
                  type="submit"
                  disabled={!canSubmit}
                  className="group flex min-h-12 w-full items-center justify-center gap-2 rounded-chip bg-action px-4 py-3 text-sm font-semibold text-action-ink disabled:cursor-not-allowed"
                >
                  {submitting ? "Signing in…" : "Sign in"}
                  {!submitting && (
                    <svg
                      viewBox="0 0 24 24"
                      fill="none"
                      stroke="currentColor"
                      strokeWidth={1.75}
                      strokeLinecap="round"
                      strokeLinejoin="round"
                      className="h-4 w-4 transition-transform duration-200 group-hover:translate-x-0.5"
                      aria-hidden="true"
                      focusable="false"
                    >
                      <path d="M5 12h13m-5-5 5 5-5 5" />
                    </svg>
                  )}
                </button>
              </form>
            </LoginCard>
            </div>
          </div>
        </div>
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
