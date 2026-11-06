"use client";

/**
 * Client-side session bootstrap and guard.
 *
 * A client guard, not Next.js middleware, because the session lives in an
 * HttpOnly cookie scoped to the API's origin: middleware running on the web
 * origin cannot read or validate it, so it would have to guess. Asking
 * `GET /auth/me` is the only honest check, and the API enforces the real
 * boundary regardless of what this component renders.
 *
 * It gates the UI, it does not gate the data. Every protected endpoint still
 * returns 401 on its own.
 */

import { usePathname, useRouter } from "next/navigation";
import { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";
import { ApiError, api, type ApiUser } from "@/lib/api";
import { ShellPlaceholder } from "./ShellPlaceholder";

interface AuthState {
  user: ApiUser | null;
  loading: boolean;
  signOut: () => Promise<void>;
}

const AuthContext = createContext<AuthState>({
  user: null,
  loading: true,
  signOut: async () => {},
});

export function useAuth(): AuthState {
  return useContext(AuthContext);
}

export function AuthGuard({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();
  const [user, setUser] = useState<ApiUser | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  const path = useRef(pathname);
  path.current = pathname;
  const known = useRef(false);

  // Held in refs so the session effects depend only on what should re-run
  // them (mount, Retry, a route change), never on a router object's identity.
  const routerRef = useRef(router);
  routerRef.current = router;
  const toSignIn = useCallback(() => {
    routerRef.current.replace(`/login?next=${encodeURIComponent(path.current || "/")}`);
  }, []);

  // The blocking check: once on mount, and again on Retry. Only this one
  // shows the loading frame, because there is no confirmed user yet.
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    api
      .me()
      .then((result) => {
        if (cancelled) return;
        known.current = true;
        setUser(result);
      })
      .catch((cause) => {
        if (cancelled) return;
        setUser(null);
        if (cause instanceof ApiError && cause.status === 401) toSignIn();
        else setError("Could not check your session. Please try again.");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [toSignIn, attempt]);

  // Navigation re-validates quietly. The page stays on screen; a session
  // that has ended still sends the person to sign in, exactly as before.
  // A transient failure keeps the confirmed user: the API refuses every
  // protected request on its own, so nothing is unlocked by staying put.
  useEffect(() => {
    if (!known.current) return;
    let cancelled = false;
    api.me().then(
      (result) => {
        if (!cancelled) setUser(result);
      },
      (cause) => {
        if (cancelled || !(cause instanceof ApiError) || cause.status !== 401) return;
        known.current = false;
        setUser(null);
        toSignIn();
      },
    );
    return () => {
      cancelled = true;
    };
  }, [pathname, toSignIn]);

  const signOut = useCallback(async () => {
    try {
      await api.logout();
    } finally {
      // Clear local state even if the call failed: the user asked to leave,
      // and the next protected request will 401 regardless.
      setUser(null);
      router.replace("/login");
    }
  }, [router]);

  if (loading && !user) return <ShellPlaceholder />;

  if (!user) {
    if (error) {
      return (
        <div role="alert" className="p-8 text-sm text-ink-soft">
          <p>{error}</p>
          <button type="button" className="mt-3 text-accent underline" onClick={() => setAttempt((value) => value + 1)}>
            Retry
          </button>
        </div>
      );
    }
    // The redirect is already in flight; render nothing rather than a flash
    // of protected chrome.
    return null;
  }

  return (
    <AuthContext.Provider value={{ user, loading, signOut }}>
      {children}
    </AuthContext.Provider>
  );
}
