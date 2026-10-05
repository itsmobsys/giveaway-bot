/**
 * Liveness + readiness probe.
 *
 * Deliberately cheap: reports whether the process is up and whether the database
 * answers. Used by the Docker healthcheck and by Render.
 *
 * Returns 503 when the database is unreachable so an orchestrator actually
 * restarts a broken instance instead of reporting a false healthy.
 */

import { first } from "@/lib/db";
import { authConfigStatus } from "@/lib/auth";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export async function GET(): Promise<Response> {
  const startedAt = Date.now();
  // Presence only, never values: lets an operator verify env propagation
  // with curl instead of guessing which of the four keys Vercel dropped.
  const auth = authConfigStatus();
  const authStatus = {
    session_secret_ok: auth.sessionSecretOk,
    client_id: auth.clientId,
    client_secret: auth.clientSecret,
    redirect_uri: auth.redirectUri,
  };
  try {
    const row = await first<{ n: number }>("SELECT COUNT(*) AS n FROM guilds");
    return Response.json(
      {
        ok: true,
        database: "reachable",
        auth: authStatus,
        latency_ms: Date.now() - startedAt,
      },
      { headers: { "Cache-Control": "no-store" } },
    );
  } catch (error) {
    return Response.json(
      {
        ok: false,
        database: "unreachable",
        auth: authStatus,
        // Never echo the driver error to an unauthenticated caller: a Turso failure
    // embeds the database hostname and a local SQLite failure embeds a filesystem
    // path, which tells a scanner exactly which backend is in use and how it is
    // misconfigured. The detail goes to the log instead.
    error: "unreachable",
    ...(process.env.NODE_ENV !== "production" && {
      detail: error instanceof Error ? error.message : "unknown error",
    }),
      },
      { status: 503, headers: { "Cache-Control": "no-store" } },
    );
  }
}