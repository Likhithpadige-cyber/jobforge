import type { IconName } from "@/components/Icon";

export type NavItem = { href: string; label: string; icon: IconName };

export const PRIMARY_NAV: NavItem[] = [
  { href: "/", label: "Dashboard", icon: "overview" },
  { href: "/applications", label: "Applications", icon: "applications" },
  { href: "/resume-studio", label: "Resume", icon: "resume" },
  { href: "/interviews", label: "Interviews", icon: "interviews" },
  { href: "/agent-activity", label: "AI Agents", icon: "activity" },
  { href: "/analytics", label: "Analytics", icon: "analytics" },
  { href: "/settings", label: "Settings", icon: "settings" },
];

export const SECONDARY_NAV: NavItem[] = [
  { href: "/job-feed", label: "Job Feed", icon: "feed" },
  { href: "/approvals", label: "Approvals", icon: "approvals" },
  { href: "/profile", label: "Profile & Facts", icon: "profile" },
];

export const MOBILE_NAV = PRIMARY_NAV.filter(item => ["/", "/applications", "/resume-studio", "/agent-activity"].includes(item.href));

export function isNavActive(pathname: string, href: string) {
  return href === "/" ? pathname === "/" : pathname === href || pathname.startsWith(`${href}/`);
}
