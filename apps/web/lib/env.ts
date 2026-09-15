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

export const env = {
  apiBaseUrl: required(
    "NEXT_PUBLIC_API_BASE_URL",
    process.env.NEXT_PUBLIC_API_BASE_URL,
    "http://localhost:8000",
  ),
  apiPrefix: process.env.NEXT_PUBLIC_API_PREFIX ?? "/api/v1",
  appName: process.env.NEXT_PUBLIC_APP_NAME ?? "Jobforge",
} as const;
