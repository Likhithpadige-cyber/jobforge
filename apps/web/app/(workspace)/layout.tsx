import { WorkspaceShell } from "@/components/WorkspaceShell";

/**
 * Every signed-in page shares this layout, so the session guard, sidebar
 * and navigation mount once and persist across navigation. `/login` lives
 * outside the group and never renders the protected frame.
 */
export default function WorkspaceLayout({ children }: { children: React.ReactNode }) {
  return <WorkspaceShell>{children}</WorkspaceShell>;
}
