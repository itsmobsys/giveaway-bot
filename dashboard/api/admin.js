import { timingSafeEqual } from "node:crypto";
import { card, db, send } from "./_lib/turso.js";

// Default password so the panel works with zero extra setup; override with
// the ADMIN_PASSWORD env var. Note: anyone who can read this repo (or its
// Vercel env settings) can see it — it keeps curious visitors out, nothing more.
const PASSWORD = process.env.ADMIN_PASSWORD || "duggalbadmoshnahirahalol";

/** Rows per page cap — the panel pages through everything else. */
const MAX_PAGE = 100;

function authorized(pw) {
  const a = Buffer.from(String(pw ?? ""));
  const b = Buffer.from(PASSWORD);
  return a.length === b.length && timingSafeEqual(a, b);
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
      sql: `SELECT id, prize, winner_count, ends_at, ended_at, status, image_url, host_name
            FROM simple_giveaways
            WHERE status != 'active'
            ORDER BY ended_at DESC
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
            WHERE giveaway_id IN (${rows.map(() => "?").join(",")})
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

/** Delete one giveaway + its entries. Live ones are refused. */
async function deleteOne(id) {
  const rs = await db().execute({
    sql: "SELECT id, status, prize FROM simple_giveaways WHERE id = ? LIMIT 1",
    args: [id],
  });
  const gw = rs.rows?.[0];
  if (!gw) return { id, ok: false, status: 404, error: "Giveaway not found" };
  if (gw.status === "active") {
    return { id, ok: false, status: 409, error: "Still live — end it in Discord first." };
  }
  await db().execute({ sql: "DELETE FROM simple_entries WHERE giveaway_id = ?", args: [id] });
  await db().execute({ sql: "DELETE FROM simple_giveaways WHERE id = ?", args: [id] });
  return { id, ok: true, prize: gw.prize };
}

export default async function handler(req, res) {
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
  if (!authorized(pw)) return send(res, 401, { error: "Wrong password" }, "no-store");

  const { action } = body || {};

  try {
    if (action === "ping") return send(res, 200, { ok: true }, "no-store");

    if (action === "list") {
      const limit = Math.max(1, Math.min(MAX_PAGE, Number(body?.limit) || 25));
      const offset = Math.max(0, Number(body?.offset) || 0);
      return send(res, 200, await listPrevious(limit, offset, Date.now()), "no-store");
    }

    if (action === "delete") {
      // Bulk: { ids: [...] }. Partial success is a 200 with a `failed` list —
      // a race with the bot ending a giveaway shouldn't fail the whole batch.
      const ids = (Array.isArray(body?.ids) ? body.ids : []).slice(0, MAX_PAGE).map(String);
      if (ids.length) {
        const results = [];
        for (const id of ids) results.push(await deleteOne(id));
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
      const r = await deleteOne(id);
      if (!r.ok) return send(res, r.status, { error: r.error }, "no-store");
      return send(res, 200, { deleted: r.id, prize: r.prize }, "no-store");
    }
  } catch (err) {
    return send(res, err.statusCode || 500, { error: err.message || "DB error" }, "no-store");
  }

  return send(res, 400, { error: "Unknown action" }, "no-store");
}
