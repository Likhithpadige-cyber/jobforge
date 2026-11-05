/**
 * Initials for the signed-in user's avatar (M13 Phase 4).
 *
 * The session (`GET /auth/me`) carries only the user's own email and role;
 * there is no display name. The profile's `full_name` is the candidate's,
 * not necessarily the signed-in user's, so it is never used here: a
 * reviewer must not be shown the candidate's identity as their own. The
 * email's local part ("demo.reviewer" → "DR") is the user's own name as
 * far as the session knows it, and needs no extra request.
 */

const SEPARATORS = /[._\-+\s]+/;

/** Up to two uppercase initials, or null when nothing usable exists. */
export function initialsFor(user: { email?: string | null } | null | undefined): string | null {
  const local = user?.email?.split("@")[0]?.trim() ?? "";
  // Only letters make sensible initials; digits and symbols are skipped.
  const words = local.split(SEPARATORS).map((word) => word.replace(/[^\p{L}]/gu, "")).filter(Boolean);
  if (words.length === 0) return null;
  const chars = words.length === 1 ? [...words[0]!].slice(0, 1) : [[...words[0]!][0]!, [...words[words.length - 1]!][0]!];
  return chars.join("").toLocaleUpperCase();
}
