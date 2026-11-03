"use client";

/**
 * Server-authoritative operator state for every page (M12 Phase 3).
 *
 * Holds only what the API last confirmed: the generation-pause status and the
 * unresolved alert count. Nothing here is ever set optimistically. After a
 * resolution the caller asks for `refresh()`, and the banner changes only if
 * the server says so. A failed refresh keeps the last confirmed value and
 * marks it stale; it never turns a pause into "resumed".
 *
 * Owners only. For any other role nothing is fetched, because the endpoints
 * are owner-only and the API enforces that regardless of this component.
 */

import { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";
import { useAuth } from "@/components/AuthGuard";
import { operations, type PauseStatus } from "@/lib/operations";

export const REFRESH_INTERVAL_MS = 30_000;

export interface OperationsState {
  /** True only for an owner session; nothing else is meaningful otherwise. */
  enabled: boolean;
  pause: PauseStatus | null;
  /** The last pause refresh failed; `pause` is the previous confirmed value. */
  pauseStale: boolean;
  openAlerts: number | null;
  confirmedAt: number | null;
  refresh: () => Promise<void>;
}

const DISABLED: OperationsState = {
  enabled: false,
  pause: null,
  pauseStale: false,
  openAlerts: null,
  confirmedAt: null,
  refresh: async () => {},
};

const OperationsContext = createContext<OperationsState>(DISABLED);

export function useOperations(): OperationsState {
  return useContext(OperationsContext);
}

export function OperationsProvider({ children }: { children: React.ReactNode }) {
  const { user } = useAuth();
  const enabled = user?.role === "owner";
  const [pause, setPause] = useState<PauseStatus | null>(null);
  const [pauseStale, setPauseStale] = useState(false);
  const [openAlerts, setOpenAlerts] = useState<number | null>(null);
  const [confirmedAt, setConfirmedAt] = useState<number | null>(null);
  const mounted = useRef(true);

  const refresh = useCallback(async () => {
    if (!enabled) return;
    const [pauseResult, summaryResult] = await Promise.allSettled([
      operations.pauseStatus(),
      operations.summary(),
    ]);
    if (!mounted.current) return;
    if (pauseResult.status === "fulfilled") {
      setPause(pauseResult.value);
      setPauseStale(false);
      setConfirmedAt(Date.now());
    } else {
      setPauseStale(true); // keep the last confirmed value
    }
    if (summaryResult.status === "fulfilled") {
      setOpenAlerts(summaryResult.value.open_alerts.total);
    }
  }, [enabled]);

  useEffect(() => {
    mounted.current = true;
    if (!enabled) return () => {
      mounted.current = false;
    };
    void refresh();
    const timer = window.setInterval(() => void refresh(), REFRESH_INTERVAL_MS);
    const onFocus = () => void refresh();
    const onVisible = () => {
      if (document.visibilityState === "visible") void refresh();
    };
    window.addEventListener("focus", onFocus);
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      mounted.current = false;
      window.clearInterval(timer);
      window.removeEventListener("focus", onFocus);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [enabled, refresh]);

  const value: OperationsState = enabled
    ? { enabled, pause, pauseStale, openAlerts, confirmedAt, refresh }
    : DISABLED;
  return <OperationsContext.Provider value={value}>{children}</OperationsContext.Provider>;
}
