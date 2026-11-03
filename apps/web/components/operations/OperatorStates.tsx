"use client";

import type { ReactNode } from "react";
import { useAuth } from "@/components/AuthGuard";
import { EmptyState } from "@/components/EmptyState";
import { SkeletonRow } from "@/components/Skeleton";
import type { LoadError } from "@/lib/operations";

export function UnauthorizedState() {
  return (
    <div data-testid="operations-unauthorized">
      <EmptyState
        title="Owner access required"
        body="Operations views show system-wide incidents and controls, so only the workspace owner can open them."
      />
    </div>
  );
}

/**
 * Renders operator views for owners only, without calling any owner endpoint
 * for anyone else. A UI convenience: the API enforces the same rule.
 */
export function OwnerOnly({ children }: { children: ReactNode }) {
  const { user } = useAuth();
  if (user?.role !== "owner") return <UnauthorizedState />;
  return <>{children}</>;
}

export function LoadingState({ label }: { label: string }) {
  return (
    <div role="status" aria-label={label} data-testid="operations-loading" className="rounded-card border border-edge bg-surface">
      <SkeletonRow />
      <SkeletonRow />
      <SkeletonRow />
    </div>
  );
}

export function ErrorState({ error, onRetry }: { error: LoadError; onRetry: () => void }) {
  if (error.kind === "unauthorized") return <UnauthorizedState />;
  if (error.kind === "not_found") {
    return (
      <div role="alert" data-testid="operations-not-found">
        <EmptyState title="Not found" body={error.message} />
      </div>
    );
  }
  return (
    <div role="alert" data-testid="operations-error">
      <EmptyState
        title="Could not load this view"
        body={error.message}
        action={
          <button type="button" onClick={onRetry} className="inline-flex min-h-11 items-center rounded-chip border border-edge bg-surface px-4 text-sm font-semibold text-violet-deep hover:border-violet/40">
            Try again
          </button>
        }
      />
    </div>
  );
}
