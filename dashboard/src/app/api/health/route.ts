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

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export async function GET(): Promise<Response> {
  const startedAt = Date.now();
  try {
    const row = await first<{ n: number }>("SELECT COUNT(*) AS n FROM guilds");
    return Response.json(
      {
        ok: true,
        database: "reachable",
        latency_ms: Date.now() - startedAt,
      },
      { headers: { "Cache-Control": "no-store" } },
    );
  } catch (error) {
    return Response.json(
      {
        ok: false,
        database: "unreachable",
        error: error instanceof Error ? error.message : "unknown error",
      },
      { status: 503, headers: { "Cache-Control": "no-store" } },
    );
  }
}