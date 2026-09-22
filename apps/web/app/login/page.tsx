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
 * The page is dressed - artwork, glass, a gradient button - but nothing in
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
    <main className="relative flex min-h-screen flex-col overflow-hidden">
      <LoginScene />

      <div className="relative flex min-h-screen flex-col px-4 py-6 sm:px-8 sm:py-8">
        <div className="hidden sm:block">
          <BrandLockup size="corner" />
        </div>

        <div className="flex flex-1 items-center justify-center py-8">
          {/* Below md the card simply centres on the scene. From md up it is
              positioned against the artwork: 44vh puts its top just under the
              brick cap, and 34vw keeps it a little narrower than the pillar
              face behind it, so it always sits on brick rather than sky. The
              min() is a short-viewport guard - on a window too short for the
              card at 44vh, the card rises instead of being clipped. */}
          <div className="w-full max-w-[26rem] md:absolute md:left-1/2 md:top-[min(44vh,calc(100vh-30rem))] md:w-[clamp(20rem,34vw,38rem)] md:max-w-none md:-translate-x-1/2">
            <LoginCard>
              <h1 className="sr-only">Sign in to JobForge</h1>

              <div className="flex justify-center">
                <BrandLockup size="card" />
              </div>

              <p className="mt-3 text-center text-[0.8125rem] leading-5 text-ink-soft">
                {totpRequired
                  ? "JobForge requires your password and an authenticator code."
                  : "Local development: your password is enough."}
              </p>

              <form onSubmit={onSubmit} className="mt-5 space-y-3" noValidate>
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
                    className="rounded-card border border-signal-risk/25 bg-signal-risk/[0.08] px-3 py-2 text-sm text-signal-risk"
                  >
                    {error}
                  </p>
                )}

                <button
                  type="submit"
                  disabled={!canSubmit}
                  className="group flex w-full items-center justify-center gap-2 rounded-[0.875rem] bg-gradient-to-r from-[#4A72E8] to-[#7B5AD1] px-3 py-2.5 text-sm font-semibold text-white shadow-[0_12px_28px_-12px_rgba(74,114,232,0.9)] transition hover:from-[#3F66DE] hover:to-[#6E4DC7] disabled:cursor-not-allowed disabled:opacity-55 disabled:shadow-none"
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
