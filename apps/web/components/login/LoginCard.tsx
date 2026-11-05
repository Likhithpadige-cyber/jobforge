import type { ReactNode } from "react";

/** Solid surfaces keep form contrast independent of decorative artwork. */
export function LoginCard({ children }: { children: ReactNode }) {
  return <div className="w-full rounded-[1.75rem] border border-control bg-surface p-6 shadow-raised sm:p-8">{children}</div>;
}
