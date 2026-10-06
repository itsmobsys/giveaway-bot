import { card, cors, db, placeholders, send } from "./_lib/turso.js";

/** How many usernames one card may show (privacy + payload size). */
const NAME_LIMIT = 100;

const CARD_COLUMNS = `id, guild_id, prize, winner_count, ends_at, ended_at, status,
         image_url, host_name`;

const LIVE_SQL = `
  SELECT ${CARD_COLUMNS}
  FROM simple_giveaways
  WHERE status = 'active' AND ($guild = '' OR guild_id = $guild)
  ORDER BY ends_at ASC LIMIT $limit`;

const PREV_SQL = `
  SELECT ${CARD_COLUMNS}
  FROM simple_giveaways
  WHERE status != 'active' AND ($guild = '' OR guild_id = $guild)
  ORDER BY ended_at DESC LIMIT $limit`;

const ONE_SQL = `
  SELECT ${CARD_COLUMNS}
  FROM simple_giveaways WHERE id = ? LIMIT 1`;

/**
 * Entrant counts and usernames for a whole page of cards.
 *
 * This used to run two queries per card (a count and a name list), so a page of
 * 35 cards cost 70 round-trips to Turso. Both are now one grouped query each,
 * and the names query ranks rows per giveaway so the 100-row cap still applies
 * per card rather than to the page as a whole.
 */
async function hydrateMany(ids) {
  const out = new Map(ids.map((id) => [String(id), { n: 0, names: [] }]));
  const unique = [...out.keys()];
  if (!unique.length) return out;
  const list = placeholders(unique.length);

  const [countRs, namesRs] = await Promise.all([
    db().execute({
      sql: `SELECT giveaway_id, COUNT(*) AS n FROM simple_entries
            WHERE giveaway_id IN (${list}) GROUP BY giveaway_id`,
      args: unique,
    }),
    db().execute({
      sql: `SELECT giveaway_id, username FROM (
              SELECT giveaway_id, username,
                     ROW_NUMBER() OVER (PARTITION BY giveaway_id ORDER BY entered_at ASC) AS rn
              FROM simple_entries WHERE giveaway_id IN (${list})
            ) WHERE rn <= ${NAME_LIMIT}`,
      args: unique,
    }),
  ]);

  for (const row of countRs.rows || []) {
    const entry = out.get(String(row.giveaway_id));
    if (entry) entry.n = Number(row.n) || 0;
  }
  // Usernames only — user ids never leave the database (privacy).
  for (const row of namesRs.rows || []) {
    const entry = out.get(String(row.giveaway_id));
    const name = String(row.username ?? "");
    if (entry && name) entry.names.push(name);
  }
  return out;
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
      const rs = await db().execute({ sql: ONE_SQL, args: [String(q.id)] });
      const gw = rs.rows?.[0];
      if (!gw) return send(res, 404, { error: "Giveaway not found" }, "no-store");
      const hydrated = await hydrateMany([gw.id]);
      const { n, names } = hydrated.get(String(gw.id));
      return send(res, 200, { now, giveaway: card(gw, n, names, now) });
    }

    const prevLimit = Math.max(1, Math.min(10, Number(q.previous_limit ?? q.previousLimit ?? 5) || 5));
    const [liveRs, prevRs] = await Promise.all([
      db().execute({ sql: LIVE_SQL, args: { guild, limit: 25 } }),
      db().execute({ sql: PREV_SQL, args: { guild, limit: prevLimit } }),
    ]);
    const rows = [...(liveRs.rows || []), ...(prevRs.rows || [])];
    const hydrated = await hydrateMany(rows.map((gw) => gw.id));
    const cards = rows.map((gw) => {
      const { n, names } = hydrated.get(String(gw.id)) || { n: 0, names: [] };
      return card(gw, n, names, now);
    });
    return send(res, 200, {
      now,
      live: cards.filter((c) => c.status === "active"),
      previous: cards.filter((c) => c.status !== "active"),
    });
  } catch (err) {
    return send(res, err.statusCode || 500, { error: err.message || "DB error" }, "no-store");
  }
}
