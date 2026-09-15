"use client";

/**
 * Sign out.
 *
 * Calls the API so the session is revoked server-side; clearing the cookie
 * alone would leave a usable session id behind.
 */

import { useState } from "react";
import { useAuth } from "./AuthGuard";

export function SignOutButton() {
  const { user, signOut } = useAuth();
  const [busy, setBusy] = useState(false);

  if (!user) return null;

  return (
    <div className="flex items-center gap-3 border-t border-edge pt-4 text-xs text-ink-soft">
      <span className="truncate">{user.email}</span>
      <button
        type="button"
        disabled={busy}
        onClick={async () => {
          setBusy(true);
          try {
            await signOut();
          } finally {
            setBusy(false);
          }
        }}
        className="rounded-chip border border-edge px-2.5 py-1 font-medium text-ink hover:bg-muted disabled:opacity-60"
      >
        {busy ? "Signing out…" : "Sign out"}
      </button>
    </div>
  );
}
