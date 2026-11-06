"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { Icon } from "./Icon";
import { MOBILE_NAV, isNavActive } from "@/lib/navigation";

export function MobileNav({ onOpenMore }: { onOpenMore: () => void }) {
  const pathname = usePathname();
  return <nav aria-label="Mobile" className="fixed inset-x-0 bottom-0 z-30 grid grid-cols-5 border-t border-edge bg-canvas px-1 pb-[env(safe-area-inset-bottom)] lg:hidden">
    {MOBILE_NAV.map(item => {
      const active = isNavActive(pathname, item.href);
      return <Link key={item.href} href={item.href} aria-current={active ? "page" : undefined} className={`relative flex min-h-[64px] flex-col items-center justify-center gap-1 text-[11px] font-medium ${active ? "text-accent" : "text-ink-soft"}`}>
        <span aria-hidden="true" className={`absolute inset-x-5 top-0 h-0.5 rounded-full bg-action transition-transform duration-[180ms] ease-out ${active ? "scale-x-100" : "scale-x-0"}`} /><Icon name={item.icon} className="h-5 w-5" /><span>{item.label === "AI Agents" ? "Agents" : item.label}</span>
      </Link>;
    })}
    <button type="button" onClick={onOpenMore} className="flex min-h-[64px] flex-col items-center justify-center gap-1 text-[11px] font-medium text-ink-soft" aria-label="More navigation" aria-haspopup="dialog"><Icon name="more" className="h-5 w-5" /><span>More</span></button>
  </nav>;
}
