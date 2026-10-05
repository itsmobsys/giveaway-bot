import { card, cors, db, send } from "./_lib/turso.js";

const LIVE_SQL = `
  SELECT id, guild_id, prize, winner_count, ends_at, ended_at, status,
         image_url, host_name
  FROM simple_giveaways
  WHERE status = 'active' AND ($guild = '' OR guild_id = $guild)
  ORDER BY ends_at ASC LIMIT $limit`;

const PREV_SQL = `
  SELECT id, guild_id, prize, winner_count, ends_at, ended_at, status,
         image_url, host_name
  FROM simple_giveaways
  WHERE status != 'active' AND ($guild = '' OR guild_id = $guild)
  ORDER BY ended_at DESC LIMIT $limit`;

const COUNT_SQL = `SELECT COUNT(*) AS n FROM simple_entries WHERE giveaway_id = ?`;
const NAMES_SQL = `SELECT username FROM simple_entries WHERE giveaway_id = ? ORDER BY entered_at ASC LIMIT 100`;

async function hydrate(gw) {
  const [countRs, namesRs] = await Promise.all([
    db().execute({ sql: COUNT_SQL, args: [gw.id] }),
    db().execute({ sql: NAMES_SQL, args: [gw.id] }),
  ]);
  const n = Number(countRs.rows?.[0]?.n ?? 0) || 0;
  // Usernames only — user ids never leave the database (privacy).
  const names = (namesRs.rows || []).map((r) => String(r.username ?? "")).filter(Boolean);
  return { n, names };
}

export default async function handler(req, res) {
  cors(res);
  if (req.method === "OPTIONS") return res.status(204).end();
  if (req.method !== "GET") return send(res, 405, { error: "GET only" }, "no-store");

  const now = Date.now();
  try {
    const q = req.query || {};
    const guild = String(q.guild_id || q.guildId || "");
    // Single-card detail for deep links: /api/giveaways?id=gw_xxx
    if (q.id) {
      const rs = await db().execute({
        sql: `SELECT id, guild_id, prize, winner_count, ends_at, ended_at, status,
                     image_url, host_name
              FROM simple_giveaways WHERE id = ? LIMIT 1`,
        args: [String(q.id)],
      });
      const gw = rs.rows?.[0];
      if (!gw) return send(res, 404, { error: "Giveaway not found" }, "no-store");
      const { n, names } = await hydrate(gw);
      return send(res, 200, { now, giveaway: card(gw, n, names, now) });
    }

    const prevLimit = Math.max(1, Math.min(10, Number(q.previous_limit ?? q.previousLimit ?? 5) || 5));
    const [liveRs, prevRs] = await Promise.all([
      db().execute({ sql: LIVE_SQL, args: { guild, limit: 25 } }),
      db().execute({ sql: PREV_SQL, args: { guild, limit: prevLimit } }),
    ]);
    const rows = [...(liveRs.rows || []), ...(prevRs.rows || [])];
    const hydrated = await Promise.all(rows.map(hydrate));
    const cards = rows.map((gw, i) => card(gw, hydrated[i].n, hydrated[i].names, now));
    return send(res, 200, {
      now,
      live: cards.filter((c) => c.status === "active"),
      previous: cards.filter((c) => c.status !== "active"),
    });
  } catch (err) {
    return send(res, err.statusCode || 500, { error: err.message || "DB error" }, "no-store");
  }
}
