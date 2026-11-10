/**
 * Client-visible configuration.
 *
 * Only NEXT_PUBLIC_* variables reach the browser. Nothing secret is ever
 * referenced here (SECURITY.md §3.3).
 */

function required(name: string, value: string | undefined, fallback: string): string {
  if (value && value.length > 0) return value;
  if (process.env.NODE_ENV === "production") {
    // Fail loudly in production rather than silently pointing at localhost.
    console.warn(`[config] ${name} is not set; falling back to ${fallback}`);
  }
  return fallback;
}

/**
 * "same-origin" makes the browser call `/api/v1/...` on whatever origin
 * served the page - the private runtime's edge proxy, reached as
 * 127.0.0.1 on the Mac or the Tailscale HTTPS name on a phone. Baked in at
 * build time like every NEXT_PUBLIC_ value.
 */
export const SAME_ORIGIN = "same-origin";

export function resolveApiBaseUrl(value: string | undefined): string {
  if (value === SAME_ORIGIN) return "";
  return required("NEXT_PUBLIC_API_BASE_URL", value, "http://localhost:8000");
}

export const env = {
  apiBaseUrl: resolveApiBaseUrl(process.env.NEXT_PUBLIC_API_BASE_URL),
  apiPrefix: process.env.NEXT_PUBLIC_API_PREFIX ?? "/api/v1",
  appName: process.env.NEXT_PUBLIC_APP_NAME ?? "Jobforge",
} as const;
