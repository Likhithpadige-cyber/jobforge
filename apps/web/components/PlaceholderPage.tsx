import Link from "next/link";
import { AppShell } from "./AppShell";
import { EmptyState } from "./EmptyState";

/**
 * A route that exists in navigation but not yet in the product.
 *
 * Says what it will do and what to use meanwhile, rather than "coming soon".
 */
export function PlaceholderPage({
  title,
  description,
  body,
}: {
  title: string;
  description: string;
  body: string;
}) {
  return (
    <AppShell title={title} description={description}>
      <div id="content" className="mx-auto max-w-3xl pt-6">
        <EmptyState
          title={`${title} is not built yet`}
          body={body}
          action={
            <Link
              href="/"
              className="inline-flex rounded-chip border border-edge bg-surface px-3 py-1.5 text-sm text-ink hover:border-brass hover:text-brass-deep"
            >
              Back to Overview
            </Link>
          }
        />
      </div>
    </AppShell>
  );
}
