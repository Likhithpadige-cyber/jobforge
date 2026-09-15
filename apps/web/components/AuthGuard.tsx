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
import { createContext, useCallback, useContext, useEffect, useState } from "react";
import { ApiError, api, type ApiUser } from "@/lib/api";

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

  useEffect(() => {
    let cancelled = false;
    api
      .me()
      .then((result) => {
        if (!cancelled) setUser(result);
      })
      .catch((cause) => {
        if (cancelled) return;
        setUser(null);
        if (cause instanceof ApiError && cause.status === 401) {
          const next = encodeURIComponent(pathname || "/");
          router.replace(`/login?next=${next}`);
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [pathname, router]);

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

  if (loading) {
    return (
      <div className="p-8 text-sm text-ink-soft" data-testid="auth-loading">
        Checking your session…
      </div>
    );
  }

  if (!user) {
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
