/**
 * Loading placeholder. Shaped like the content it replaces so the layout
 * does not jump when real data arrives.
 */
export function Skeleton({ className = "h-4 w-full" }: { className?: string }) {
  return (
    <div
      className={`animate-pulse rounded bg-muted ${className}`}
      aria-hidden="true"
    />
  );
}

export function SkeletonRow() {
  return (
    <div className="flex items-center gap-4 border-b border-edge px-4 py-3 last:border-0">
      <Skeleton className="h-4 w-40" />
      <Skeleton className="h-4 w-28" />
      <Skeleton className="ml-auto h-4 w-16" />
    </div>
  );
}
