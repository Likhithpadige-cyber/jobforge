"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import { Icon, Wordmark, type IconName } from "./Icon";
import { api, type Readiness } from "@/lib/api";

type NavItem = { href: string; label: string; icon: IconName; badge?: number };

const WORKFLOW: NavItem[] = [
  { href: "/", label: "Overview", icon: "overview" },
  { href: "/job-feed", label: "Job Feed", icon: "feed" },
  { href: "/approvals", label: "Approvals", icon: "approvals", badge: 4 },
  { href: "/resume-studio", label: "Resume Studio", icon: "resume" },
  { href: "/applications", label: "Applications", icon: "applications" },
  { href: "/agent-activity", label: "Agent Activity", icon: "activity" },
];

const WORKSPACE: NavItem[] = [
  { href: "/profile", label: "Profile & Facts", icon: "profile" },
  { href: "/settings", label: "Settings", icon: "settings" },
];

function NavLink({ item, active }: { item: NavItem; active: boolean }) {
  return (
    <Link
      href={item.href}
      aria-current={active ? "page" : undefined}
      className={[
        "group relative flex items-center gap-3 rounded-chip py-2 pl-4 pr-3 text-sm transition-colors",
        active
          ? "bg-graphite font-medium text-white shadow-[inset_0_0_0_0.5px_rgba(255,255,255,0.06)]"
          : "text-white/55 hover:bg-white/[0.06] hover:text-white",
      ].join(" ")}
    >
      {/* A thin brass rule marks the current page rather than a saturated
          block of colour. */}
      <span
        className={`absolute left-0 top-1 bottom-1 w-[3px] rounded-r-full bg-brass transition-opacity ${
          active ? "opacity-100" : "opacity-0"
        }`}
        aria-hidden="true"
      />
      <Icon name={item.icon} className="h-4 w-4 shrink-0" />
      <span className="truncate">{item.label}</span>
      {item.badge ? (
        <span className="tabular ml-auto rounded-chip bg-brass/15 px-1.5 py-0.5 text-xs font-medium text-brass">
          {item.badge}
        </span>
      ) : null}
    </Link>
  );
}

function SystemStatus() {
  const [health, setHealth] = useState<Readiness | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const result = await api.readiness();
        if (!cancelled) {
          setHealth(result);
          setFailed(false);
        }
      } catch {
        if (!cancelled) setFailed(true);
      }
    };
    void load();
    const timer = setInterval(() => void load(), 30_000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, []);

  const state = failed ? "unavailable" : (health?.status ?? "checking");
  const dot =
    state === "ok"
      ? "bg-signal-strong"
      : state === "degraded"
        ? "bg-signal-review"
        : state === "unavailable"
          ? "bg-signal-risk"
          : "bg-white/30";

  return (
    <div className="flex items-center gap-2 px-3 py-1.5 text-xs text-white/45">
      <span className={`h-1.5 w-1.5 rounded-full ${dot}`} aria-hidden="true" />
      <span>API {state}</span>
      {health?.migration_revision && (
        <span className="ml-auto font-mono text-[0.65rem] text-white/30">
          {health.migration_revision}
        </span>
      )}
    </div>
  );
}

export function Sidebar({ onNavigate }: { onNavigate?: () => void }) {
  const pathname = usePathname();
  const isActive = (href: string) =>
    href === "/" ? pathname === "/" : pathname.startsWith(href);

  return (
    <div className="on-dark flex h-full flex-col bg-charcoal">
      <div className="px-4 pb-6 pt-5">
        <Link href="/" onClick={onNavigate} className="inline-block rounded-chip">
          <Wordmark />
        </Link>
      </div>

      <nav className="flex-1 space-y-7 px-3" aria-label="Main">
        <div className="space-y-1" onClick={onNavigate}>
          {WORKFLOW.map((item) => (
            <NavLink key={item.href} item={item} active={isActive(item.href)} />
          ))}
        </div>

        <div>
          <p className="px-3 pb-1.5 text-xs text-white/35">Workspace</p>
          <div className="space-y-1" onClick={onNavigate}>
            {WORKSPACE.map((item) => (
              <NavLink key={item.href} item={item} active={isActive(item.href)} />
            ))}
          </div>
        </div>
      </nav>

      <div className="mt-6 border-t border-white/[0.08] p-3">
        <SystemStatus />
        <div className="mt-1 flex items-center gap-3 rounded-chip px-3 py-2.5">
          <span
            className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-brass/15 text-xs font-medium text-brass"
            aria-hidden="true"
          >
            LS
          </span>
          <div className="min-w-0">
            <p className="truncate text-sm text-white/90">Likhith Sai</p>
            <p className="truncate text-xs text-white/40">Cloud Engineer</p>
          </div>
        </div>
      </div>
    </div>
  );
}

export { WORKFLOW as NAV_WORKFLOW, WORKSPACE as NAV_WORKSPACE };
