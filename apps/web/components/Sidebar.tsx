"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { Icon, Wordmark } from "./Icon";
import { PRIMARY_NAV, SECONDARY_NAV, isNavActive, type NavItem } from "@/lib/navigation";

function NavLink({ item, active, onNavigate }: { item: NavItem; active: boolean; onNavigate?: () => void }) {
  return (
    <Link href={item.href} onClick={onNavigate} aria-current={active ? "page" : undefined}
      className={`group relative flex items-center gap-3 rounded-chip px-3.5 py-3 text-sm font-medium transition-[color,background-color,transform] duration-200 hover:translate-x-0.5 ${active ? "bg-violet-soft text-violet-deep" : "text-ink-soft hover:bg-muted hover:text-ink"}`}>
      <span aria-hidden="true" className={`absolute bottom-2 left-0 top-2 w-[3px] rounded-r-full bg-violet transition-transform duration-300 ${active ? "scale-y-100" : "scale-y-0"}`} />
      <Icon name={item.icon} className="h-[18px] w-[18px] shrink-0" />
      <span>{item.label}</span>
    </Link>
  );
}

export function Sidebar({ onNavigate }: { onNavigate?: () => void }) {
  const pathname = usePathname();
  return (
    <div className="flex h-full flex-col border-r border-edge bg-surface">
      <div className="px-6 pb-8 pt-7">
        <Link href="/" onClick={onNavigate} className="inline-flex rounded-chip"><Wordmark /></Link>
        <p className="mt-2 pl-[34px] text-[11px] font-medium tracking-[0.13em] text-ink-soft">CAREER WORKSPACE</p>
      </div>
      <nav className="flex-1 space-y-7 overflow-y-auto px-3" aria-label="Main">
        <div className="space-y-1">{PRIMARY_NAV.map(item => <NavLink key={item.href} item={item} active={isNavActive(pathname, item.href)} onNavigate={onNavigate} />)}</div>
        <div className="border-t border-edge pt-5">
          <p className="px-3.5 pb-2 text-[11px] font-semibold uppercase tracking-[0.12em] text-ink-soft">Workspace</p>
          <div className="space-y-1">{SECONDARY_NAV.map(item => <NavLink key={item.href} item={item} active={isNavActive(pathname, item.href)} onNavigate={onNavigate} />)}</div>
        </div>
      </nav>
      <div className="m-3 rounded-card border border-violet/10 bg-violet-soft/60 p-4">
        <p className="text-xs font-semibold text-violet-deep">Your next step</p>
        <p className="mt-1 text-sm text-ink">Review what is waiting for your approval.</p>
        <Link href="/approvals" onClick={onNavigate} className="mt-3 inline-flex items-center gap-1 text-xs font-semibold text-violet-deep hover:underline">View approvals <Icon name="arrow" className="h-3 w-3" /></Link>
      </div>
    </div>
  );
}

export { PRIMARY_NAV as NAV_WORKFLOW, SECONDARY_NAV as NAV_WORKSPACE };
