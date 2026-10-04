/**
 * Live giveaway updates via Server-Sent Events.
 *
 * SSE is used rather than WebSockets because it works with serverless-style
 * runtimes and needs no extra infrastructure. The bot writes rows to
 * `giveaway_events`; this endpoint tails them and pushes them to the browser.
 *
 * Robustness notes:
 *  - a heartbeat every 15s keeps proxies from closing an idle connection
 *  - `retry:` tells the browser how long to wait before reconnecting
 *  - the stream always closes cleanly on abort, so nothing leaks
 */

import { getPublicGiveaway, latestEventId, listEventsSince } from "@/lib/giveaways";

export const dynamic = "force-dynamic";
// Node runtime keeps the connection alive reliably on Vercel and Render.
export const runtime = "nodejs";

const HEARTBEAT_MS = 15_000;
const MAX_DURATION_MS = 55_000; // stay under typical serverless limits
const POLL_MS = 2_000;

export async function GET(
  request: Request,
  { params }: { params: Promise<{ id: string }> },
): Promise<Response> {
  const { id } = await params;
  const giveaway = await getPublicGiveaway(id);
  if (!giveaway) {
    return Response.json({ error: "Giveaway not found" }, { status: 404 });
  }

  const encoder = new TextEncoder();
  let cursor = await latestEventId(id);

  const stream = new ReadableStream<Uint8Array>({
    async start(controller) {
      let closed = false;
      const send = (chunk: string) => {
        if (closed) return;
        try {
          controller.enqueue(encoder.encode(chunk));
        } catch {
          // Stream already closed by the client; nothing to do.
          closed = true;
        }
      };

      // Tell the browser how to reconnect, then send the current snapshot so a
      // fresh subscriber is immediately correct.
      send("retry: 3000\n\n");
      send(`event: snapshot\ndata: ${JSON.stringify({
        id: giveaway.id,
        status: giveaway.status,
        participant_count: giveaway.participant_count,
        entry_count: giveaway.entry_count,
        ends_at: giveaway.ends_at,
      })}\n\n`);

      const startedAt = Date.now();
      let lastHeartbeat = startedAt;

      const tick = async (): Promise<boolean> => {
        // Stop at the duration cap so the platform can recycle the function.
        if (Date.now() - startedAt > MAX_DURATION_MS) {
          send("event: timeout\ndata: {}\n\n");
          return false;
        }
        try {
          const events = await listEventsSince(id, cursor, 50);
          for (const event of events) {
            cursor = event.id;
            send(`event: update\ndata: ${JSON.stringify({
              id: event.id,
              type: event.type,
              created_at: event.created_at,
              ...(typeof event.payload === "object" && event.payload !== null
                ? event.payload
                : {}),
            })}\n\n`);
          }
        } catch (error) {
          // A transient database error must not kill the stream; the client
          // reconnects if the connection actually drops.
          console.warn("SSE poll failed:", error);
        }

        if (Date.now() - lastHeartbeat > HEARTBEAT_MS) {
          lastHeartbeat = Date.now();
          send(`: heartbeat ${Date.now()}\n\n`);
        }
        return true;
      };

      const loop = (async () => {
        try {
          while (!closed && !request.signal.aborted && (await tick())) {
            if (Date.now() - startedAt > MAX_DURATION_MS) break;
            await new Promise((resolve) => setTimeout(resolve, POLL_MS));
          }
        } catch {
          // Tick errors after the first are observed here, not unhandled.
        } finally {
          closed = true;
          try {
            controller.close();
          } catch {
            // Already closed.
          }
        }
      })();
      void loop;
    },
    cancel() {
      // The consumer disconnected; the loop observes request.signal.aborted
      // on its next tick and exits. No-op body retained for the interface.
    },
  });

  return new Response(stream, {
    headers: {
      "Content-Type": "text/event-stream; charset=utf-8",
      "Cache-Control": "no-cache, no-transform",
      Connection: "keep-alive",
      // Tell nginx-style proxies not to buffer the stream.
      "X-Accel-Buffering": "no",
    },
  });
}