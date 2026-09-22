import type { IconName } from "@/components/Icon";

export type DashboardSignals = {
  resumesWaitingReview?: number;
  interviewsWithinDay?: number;
  applicationsThisWeek?: number;
  profileComplete?: boolean;
  approvalsWaiting: number;
  strongMatches: number;
};

export type DashboardPriority = {
  eyebrow: string;
  title: string;
  body: string;
  href: string;
  action: string;
  icon: IconName;
};

/** Present the most time-sensitive action supported by available signals. */
export function getDashboardPriority(signals: DashboardSignals): DashboardPriority {
  if ((signals.resumesWaitingReview ?? 0) > 0) return {
    eyebrow: "Needs your attention", title: "Your resume is ready for review", body: "Check the tailored draft and its verified claims before moving forward.", href: "/resume-studio", action: "Review resume", icon: "resume",
  };
  if ((signals.interviewsWithinDay ?? 0) > 0) return {
    eyebrow: "Coming up soon", title: "Your interview is tomorrow", body: "Take a few minutes to prepare for the conversation.", href: "/interviews", action: "Prepare for interview", icon: "interviews",
  };
  if (signals.applicationsThisWeek === 0) return {
    eyebrow: "Build momentum", title: "Find your next strong match", body: "Explore roles that fit your verified experience.", href: "/job-feed", action: "Explore jobs", icon: "feed",
  };
  if (signals.profileComplete === false) return {
    eyebrow: "Strengthen your profile", title: "A few details can improve your matches", body: "Confirm your experience so JobForge can represent you accurately.", href: "/profile", action: "Complete profile", icon: "profile",
  };
  if (signals.approvalsWaiting > 0) return {
    eyebrow: "Needs your attention", title: `${signals.approvalsWaiting} applications are ready for your review`, body: "Your approval is the final step. Nothing is sent before you say so.", href: "/approvals", action: "Review applications", icon: "approvals",
  };
  if (signals.strongMatches > 0) return {
    eyebrow: "New opportunities", title: `${signals.strongMatches} strong matches are waiting`, body: "Explore the roles that best match your verified experience.", href: "/job-feed", action: "Explore matches", icon: "feed",
  };
  return { eyebrow: "Keep moving", title: "Build your next opportunity", body: "Start with your profile and let JobForge find roles worth your time.", href: "/profile", action: "View profile", icon: "profile" };
}
