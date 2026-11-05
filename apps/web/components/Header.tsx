"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useState } from "react";
import { Icon } from "./Icon";
import { PRIMARY_NAV, SECONDARY_NAV } from "@/lib/navigation";
import { useAuth } from "./AuthGuard";
import { useOperations } from "./operations/OperationsProvider";
import { initialsFor } from "@/lib/initials";

const SEARCH_PAGES = [...PRIMARY_NAV, ...SECONDARY_NAV];

export function Header({ title, description, onOpenNav }: { title: string; description?: string; onOpenNav?: () => void }) {
  const pathname = usePathname();
  const router = useRouter();
  const [query, setQuery] = useState("");
  const [focused, setFocused] = useState(false);
  const results = query.trim() ? SEARCH_PAGES.filter(item => item.label.toLowerCase().includes(query.trim().toLowerCase())) : [];
  const segment = pathname.split("/").filter(Boolean);
  const { enabled: isOwner, openAlerts } = useOperations();

  return (
    <header className="sticky top-0 z-30 border-b border-edge bg-canvas">
      <div className="flex min-h-[76px] items-center gap-3 px-5 lg:px-9">
        <button type="button" onClick={onOpenNav} className="rounded-chip border border-edge bg-surface p-2 text-ink lg:hidden" aria-label="Open navigation" aria-haspopup="dialog"><Icon name="feed" className="h-5 w-5" /></button>
        <div className="min-w-0 flex-1">
          {segment.length > 0 && <nav aria-label="Breadcrumb" className="mb-0.5 flex items-center gap-1.5 text-xs text-ink-soft"><Link href="/" className="inline-flex min-h-6 items-center hover:text-accent">Dashboard</Link><span aria-hidden="true">/</span><span className="truncate">{title}</span></nav>}
          <h1 className="truncate text-lg font-semibold tracking-tight text-ink lg:text-xl">{title}</h1>
          {description && <p className="hidden truncate text-xs text-ink-soft xl:block">{description}</p>}
        </div>
        <div className="relative hidden w-64 md:block lg:w-72">
          <form onSubmit={event => { event.preventDefault(); if (results[0]) { router.push(results[0].href); setFocused(false); setQuery(""); } }}>
            <label htmlFor="global-search" className="sr-only">Search pages</label>
            <Icon name="search" className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-ink-soft" />
            <input id="global-search" type="search" autoComplete="off" placeholder="Search your workspace" value={query} onChange={event => setQuery(event.target.value)} onFocus={() => setFocused(true)} onBlur={() => window.setTimeout(() => setFocused(false), 150)} className="h-10 w-full rounded-chip border border-edge bg-surface pl-9 pr-3 text-sm text-ink placeholder:text-ink-soft focus:border-control" />
          </form>
          {focused && query.trim() && <div className="absolute left-0 right-0 top-[calc(100%+8px)] overflow-hidden rounded-card border border-edge bg-surface p-1 shadow-raised" role="listbox" aria-label="Search results">
            {results.length ? results.map(item => <Link key={item.href} role="option" aria-selected="false" href={item.href} onClick={() => { setFocused(false); setQuery(""); }} className="flex items-center gap-2 rounded-chip px-3 py-2 text-sm text-ink hover:bg-inset"><Icon name={item.icon} />{item.label}</Link>) : <p className="px-3 py-2 text-sm text-ink-soft">No pages found</p>}
          </div>}
        </div>
        {isOwner && <AlertBell count={openAlerts} />}
        <AccountAvatar />
      </div>
    </header>
  );
}

/**
 * The signed-in user's own initials, from the session only. The owner's
 * avatar opens their profile; a reviewer's does not, because that profile
 * is the candidate's, not theirs.
 */
function AccountAvatar() {
  const { user } = useAuth();
  if (!user) return null;
  const initials = initialsFor(user);
  const className = "flex h-11 w-11 items-center justify-center rounded-full border border-edge bg-inset text-xs font-semibold text-accent";
  const content = initials ?? <Icon name="profile" className="h-5 w-5" />;
  const who = `Signed in as ${user.email}${user.role === "owner" ? "" : ` (${user.role})`}`;
  return user.role === "owner" ? (
    <Link href="/profile" data-testid="account-avatar" className={`${className} hover:border-control`} aria-label={`${initials ? `${initials}. ` : ""}Your profile. ${who}`} title={who}>
      <span aria-hidden="true">{content}</span>
    </Link>
  ) : (
    <span data-testid="account-avatar" role="img" className={className} aria-label={who} title={who}>
      <span aria-hidden="true">{content}</span>
    </span>
  );
}

/** The real unresolved alert count from the API; no number until it is known. */
function AlertBell({ count }: { count: number | null }) {
  const label =
    count === null
      ? "Alerts"
      : count === 0
        ? "Alerts: none unresolved"
        : `Alerts: ${count} unresolved`;
  return (
    <Link href="/alerts" data-testid="alert-bell" className="relative rounded-chip border border-edge bg-surface p-2.5 text-ink-soft transition-transform hover:text-accent" aria-label={label}>
      <Icon name="bell" className="h-5 w-5" />
      {count !== null && count > 0 && (
        <span aria-hidden="true" className="absolute -right-1.5 -top-1.5 min-w-5 rounded-full bg-action px-1.5 text-center text-[11px] font-semibold leading-5 text-action-ink">
          {count > 99 ? "99+" : count}
        </span>
      )}
    </Link>
  );
}
