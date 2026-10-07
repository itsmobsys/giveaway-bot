import { createHash, timingSafeEqual } from "node:crypto";
import { card, db, placeholders, send } from "./_lib/turso.js";


/** Rows per page cap — the panel pages through everything else. */
const MAX_PAGE = 100;

function authorized(pw, password) {
  const a = createHash("sha256").update(String(pw ?? "")).digest();
  const b = createHash("sha256").update(password).digest();
  return timingSafeEqual(a, b);
}

/**
 * Every finished giveaway, newest first, paged. The public /api/giveaways caps
 * previous at 10, which is fine for a wall of cards but useless for cleaning
 * up — the panel needs the whole list. One grouped query counts entrants for
 * the page instead of one query per row.
 */
async function listPrevious(limit, offset, now) {
  const [rowsRs, totalRs, liveRs] = await Promise.all([
    db().execute({
      sql: `SELECT id, prize, winner_count, ends_at,
                   COALESCE(ended_at, created_at) AS ended_at, entrant_count,
                   status, image_url, host_name
            FROM simple_giveaways
            WHERE status != 'active'
            ORDER BY COALESCE(ended_at, created_at) DESC, id DESC
            LIMIT ? OFFSET ?`,
      args: [limit, offset],
    }),
    db().execute({ sql: "SELECT COUNT(*) AS n FROM simple_giveaways WHERE status != 'active'" }),
    db().execute({ sql: "SELECT COUNT(*) AS n FROM simple_giveaways WHERE status = 'active'" }),
  ]);

  const rows = rowsRs.rows || [];
  const counts = new Map();
  if (rows.length) {
    const rs = await db().execute({
      sql: `SELECT giveaway_id, COUNT(*) AS n FROM simple_entries
            WHERE giveaway_id IN (${placeholders(rows.length)})
            GROUP BY giveaway_id`,
      args: rows.map((r) => r.id),
    });
    for (const r of rs.rows || []) counts.set(r.giveaway_id, Number(r.n) || 0);
  }

  return {
    now,
    live: Number(liveRs.rows?.[0]?.n ?? 0) || 0,
    total: Number(totalRs.rows?.[0]?.n ?? 0) || 0,
    // No usernames: the panel only needs counts.
    previous: rows.map((gw) => card(gw, counts.get(gw.id) || 0, [], now)),
  };
}

/**
 * Delete giveaways and their entries, reporting per id.
 *
 * Live giveaways are refused, and so is an id that does not exist. This used to
 * be three sequential round-trips per id, so a 100-row bulk delete spent 300
 * round-trips inside one serverless invocation and could time out halfway. It is
 * now one lookup and one transactional two-statement batch, whatever its size.
 */
async function deleteMany(ids) {
  const unique = [...new Set(ids.map(String))].filter(Boolean);
  if (!unique.length) return [];

  const list = placeholders(unique.length);
  const rs = await db().execute({
    sql: `SELECT id, status, prize FROM simple_giveaways WHERE id IN (${list})`,
    args: unique,
  });
  const found = new Map((rs.rows || []).map((r) => [String(r.id), r]));

  const results = unique.map((id) => {
    const gw = found.get(id);
    if (!gw) return { id, ok: false, status: 404, error: "Giveaway not found" };
    if (gw.status === "active") {
      return { id, ok: false, status: 409, error: "Still live — end it in Discord first." };
    }
    return { id, ok: true, prize: gw.prize };
  });

  const removable = results.filter((r) => r.ok).map((r) => r.id);
  if (removable.length) {
    const del = placeholders(removable.length);
    const [, deleted] = await db().batch([
      { sql: `DELETE FROM simple_entries WHERE giveaway_id IN (
                SELECT id FROM simple_giveaways WHERE status != 'active' AND id IN (${del})
              )`, args: removable },
      { sql: `DELETE FROM simple_giveaways WHERE status != 'active'
              AND id IN (${del}) RETURNING id`, args: removable },
    ], "write");
    const deletedIds = new Set((deleted.rows || []).map((r) => String(r.id)));
    for (const result of results) {
      if (result.ok && !deletedIds.has(result.id)) {
        result.ok = false;
        result.status = 409;
        result.error = "Giveaway is no longer eligible for deletion";
      }
    }
  }
  return results;
}

export default async function handler(req, res) {
  const password = process.env.ADMIN_PASSWORD;
  if (!password) return send(res, 503, { error: "Admin disabled: ADMIN_PASSWORD not set" }, "no-store");
  if (req.method === "OPTIONS") return res.status(204).end();
  if (req.method !== "POST") return send(res, 405, { error: "POST only" }, "no-store");

  let body = req.body;
  if (typeof body === "string") {
    try {
      body = JSON.parse(body);
    } catch {
      body = {};
    }
  }
  const pw = req.headers?.["x-admin-password"] ?? body?.password;
  if (!authorized(pw, password)) {
    await new Promise((resolve) => setTimeout(resolve, 300));
    return send(res, 401, { error: "Wrong password" }, "no-store");
  }

  const { action } = body || {};

  try {
    if (action === "ping") return send(res, 200, { ok: true }, "no-store");

    if (action === "list") {
      const requestedLimit = Number(body?.limit);
      const requestedOffset = Number(body?.offset);
      const limit = Number.isFinite(requestedLimit)
        ? Math.max(1, Math.min(MAX_PAGE, Math.trunc(requestedLimit))) : 25;
      const offset = Number.isFinite(requestedOffset)
        ? Math.max(0, Math.min(Number.MAX_SAFE_INTEGER, Math.trunc(requestedOffset))) : 0;
      return send(res, 200, await listPrevious(limit, offset, Date.now()), "no-store");
    }

    if (action === "delete") {
      // Bulk: { ids: [...] }. Partial success is a 200 with a `failed` list —
      // a race with the bot ending a giveaway should not fail the whole batch.
      const ids = (Array.isArray(body?.ids) ? body.ids : []).slice(0, MAX_PAGE).map(String);
      if (ids.length) {
        const results = await deleteMany(ids);
        const ok = results.filter((r) => r.ok);
        const failed = results.filter((r) => !r.ok);
        const status = failed.length === results.length ? failed[0].status : 200;
        return send(res, status, {
          deleted: ok.length,
          failed: failed.map((f) => ({ id: f.id, error: f.error })),
        }, "no-store");
      }

      const id = String(body?.id || "");
      if (!id) return send(res, 400, { error: "Missing giveaway id" }, "no-store");
      const [r] = await deleteMany([id]);
      if (!r.ok) return send(res, r.status, { error: r.error }, "no-store");
      return send(res, 200, { deleted: r.id, prize: r.prize }, "no-store");
    }
  } catch (err) {
    console.error("Admin API error:", err);
    return send(res, 500, { error: "Internal server error" }, "no-store");
  }

  return send(res, 400, { error: "Unknown action" }, "no-store");
}
