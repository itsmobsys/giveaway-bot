import { createHash, timingSafeEqual } from "node:crypto";
import { card, db, placeholders, send } from "./_lib/turso.js";


/** Rows per page cap — the panel pages through everything else. */
const MAX_PAGE = 100;

/** Longest search string accepted; anything past this is ignored. */
const MAX_QUERY = 100;

/**
 * Status filters the panel may ask for. `all` is every finished giveaway, which
 * is what the panel has always listed — a live row cannot be deleted from here
 * and is counted in `live` instead. Anything unrecognised falls back to `all`;
 * the value is only ever used to pick a bound-parameter arm, never interpolated.
 */
const STATUS_FILTERS = new Set(["all", "ended", "cancelled", "active"]);

function authorized(pw, password) {
  const a = createHash("sha256").update(String(pw ?? "")).digest();
  const b = createHash("sha256").update(password).digest();
  return timingSafeEqual(a, b);
}

/**
 * Every finished giveaway, newest first, paged. The public /api/giveaways caps
 * previous at 10, which is fine for a wall of cards but useless for cleaning
 * up — the panel needs the whole list, and it can search/filter it.
 *
 * One statement: page rows, the filtered total and the live count ride along as
 * scalar subqueries, and a legacy NULL entrant_count is filled from
 * simple_entries in the projection. This used to be four round-trips (rows,
 * total, live, then an entry-count query), all on the same page.
 *
 * The seed row keeps total/live readable when a filter or offset yields no
 * rows at all; the panel still needs them for its header and pager.
 */
async function listPrevious(limit, offset, status, q, now) {
  const where = [status === "all" ? "status IN ('ended', 'cancelled')" : "status = ?"];
  const args = status === "all" ? [] : [status];
  if (q) {
    where.push("(prize LIKE '%' || ? || '%' OR host_name LIKE '%' || ? || '%')");
    args.push(q, q);
  }
  const filter = where.join(" AND ");
  // The panel's `active` filter is the one view whose rows are live, and for a
  // live card the stored entrant_count can lag the entries table, so that view
  // counts rows directly. Every other row is finished and prefers its snapshot.
  const countExpr = status === "active"
    ? "(SELECT COUNT(*) FROM simple_entries e WHERE e.giveaway_id = p.id)"
    : `COALESCE(p.entrant_count, (
         SELECT COUNT(*) FROM simple_entries e WHERE e.giveaway_id = p.id
       ))`;

  const rs = await db().execute({
    sql: `WITH filtered AS (
            SELECT * FROM simple_giveaways WHERE ${filter}
          ), page AS (
            SELECT * FROM filtered
            ORDER BY COALESCE(ended_at, created_at) DESC, id DESC
            LIMIT ? OFFSET ?
          )
          SELECT p.id, p.prize, p.winner_count, p.ends_at,
                 COALESCE(p.ended_at, p.created_at) AS ended_at,
                 ${countExpr} AS entrant_count,
                 p.status, p.image_url, p.host_name, p.claim_timeout_seconds,
                 (SELECT COUNT(*) FROM filtered) AS total,
                 (SELECT COUNT(*) FROM simple_giveaways WHERE status = 'active') AS live
          FROM (SELECT 1) seed
          LEFT JOIN page p ON 1 = 1`,
    args: [...args, limit, offset],
  });

  const raw = rs.rows || [];
  const counts = raw[0];
  return {
    now,
    live: Number(counts?.live ?? 0) || 0,
    total: Number(counts?.total ?? 0) || 0,
    // No usernames: the panel only needs counts. The projected count is passed
    // as the live fallback too, so the `active` filter shows entry-row counts.
    previous: raw
      .filter((gw) => gw.id != null)
      .map((gw) => card(gw, Number(gw.entrant_count) || 0, [], now)),
  };
}

/**
 * Delete giveaways and their entries, reporting per id.
 *
 * Live giveaways are refused, and so is an id that does not exist.
 *
 * The deletes carry the status guard themselves, so a row that is gone or still
 * live simply is not deleted (and its entries/claims are left alone). Reading
 * eligibility first, as this used to, was a race against the bot ending a
 * giveaway between the read and the delete; a guarded delete cannot lose that
 * race. So the three-statement batch is the whole operation in the common case,
 * and only ids it did not return need one follow-up read to tell 404 from 409.
 * The first statement purges simple_claims so finished giveaways leave no
 * orphaned claim rows behind.
 */
async function deleteMany(ids) {
  const unique = [...new Set(ids.map(String))].filter(Boolean);
  if (!unique.length) return [];

  const list = placeholders(unique.length);
  const [, , deleted] = await db().batch([
    { sql: `DELETE FROM simple_claims WHERE giveaway_id IN (
              SELECT id FROM simple_giveaways WHERE status IN ('ended', 'cancelled') AND id IN (${list})
            )`, args: unique },
    { sql: `DELETE FROM simple_entries WHERE giveaway_id IN (
              SELECT id FROM simple_giveaways WHERE status IN ('ended', 'cancelled') AND id IN (${list})
            )`, args: unique },
    // prize comes back too: the single-id response reports it and the row no
    // longer exists to ask afterwards.
    { sql: `DELETE FROM simple_giveaways WHERE status IN ('ended', 'cancelled')
            AND id IN (${list}) RETURNING id, prize`, args: unique },
  ], "write");

  const removed = new Map((deleted.rows || []).map((r) => [String(r.id), r.prize]));
  const rest = unique.filter((id) => !removed.has(id));
  const found = new Map();
  if (rest.length) {
    const rs = await db().execute({
      sql: `SELECT id, status, prize FROM simple_giveaways WHERE id IN (${placeholders(rest.length)})`,
      args: rest,
    });
    for (const r of rs.rows || []) found.set(String(r.id), r);
  }

  return unique.map((id) => {
    if (removed.has(id)) return { id, ok: true, prize: removed.get(id) };
    const gw = found.get(id);
    if (!gw) return { id, ok: false, status: 404, error: "Giveaway not found" };
    return { id, ok: false, status: 409, error: "Still live — end it in Discord first." };
  });
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
      const status = STATUS_FILTERS.has(body?.status) ? body.status : "all";
      const q = typeof body?.q === "string" ? body.q.trim().slice(0, MAX_QUERY) : "";
      return send(res, 200, await listPrevious(limit, offset, status, q, Date.now()), "no-store");
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
