/**
 * Frontend liveness endpoint.
 *
 * Distinct from the backend health endpoints: this only says the Next.js
 * server is up, which is what the container healthcheck needs.
 */

export const dynamic = "force-dynamic";

export async function GET(): Promise<Response> {
  return Response.json({ status: "ok", service: "jobforge-web" });
}
