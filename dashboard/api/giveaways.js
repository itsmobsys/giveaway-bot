import { card, cors, db, placeholders, send } from "./_lib/turso.js";

/** How many usernames one card shows (the wall renders 8; privacy + payload). */
const NAME_LIMIT = 8;

const CARD_COLUMNS = `id, guild_id, prize, winner_count, ends_at,
         COALESCE(ended_at, created_at) AS ended_at, entrant_count, status,
         image_url, host_name, claim_timeout_seconds`;

const LIVE_SQL = `
  SELECT ${CARD_COLUMNS}
  FROM simple_giveaways
  WHERE status = 'active' AND ($guild = '' OR guild_id = $guild)
  ORDER BY ends_at ASC LIMIT $limit`;

const PREV_SQL = `
  SELECT ${CARD_COLUMNS}
  FROM simple_giveaways
  WHERE status IN ('ended', 'cancelled') AND ($guild = '' OR guild_id = $guild)
  ORDER BY COALESCE(ended_at, created_at) DESC, id DESC LIMIT $limit`;

const ONE_SQL = `
  SELECT ${CARD_COLUMNS}
  FROM simple_giveaways WHERE id = ? LIMIT 1`;

/**
 * Entrant counts and usernames for a whole page of cards.
 *
 * This used to run two queries per card (a count and a name list), so a page of
 * 35 cards cost 70 round-trips to Turso. Both are now one grouped query each,
 * and the names query ranks rows per giveaway so the 8-row cap still applies
 * per card rather than to the page as a whole.
 *
 * The counts query is skipped when every card is finished and carries its
 * final entrant_count: card() prefers that snapshot, so the round-trip would
 * tell us nothing. Live cards still need it (entries can be ahead of the stored
 * counter, which is only refreshed on the bot's side) and so do legacy rows
 * with a NULL count. Names stay for every card, because finished cards show
 * them too.
 */
async function hydrateMany(rows) {
  const out = new Map(rows.map((gw) => [String(gw.id), { n: 0, names: [] }]));
  const unique = [...out.keys()];
  if (!unique.length) return out;
  const list = placeholders(unique.length);
  const needCounts = rows.some((gw) => gw.status === "active" || gw.entrant_count == null);

  const [countRs, namesRs] = await Promise.all([
    needCounts
      ? db().execute({
          sql: `SELECT giveaway_id, COUNT(*) AS n FROM simple_entries
                WHERE giveaway_id IN (${list}) GROUP BY giveaway_id`,
          args: unique,
        })
      : null,
    db().execute({
      sql: `SELECT giveaway_id, username FROM (
              SELECT giveaway_id, username,
                     ROW_NUMBER() OVER (PARTITION BY giveaway_id ORDER BY entered_at ASC) AS rn
              FROM simple_entries WHERE giveaway_id IN (${list})
            ) WHERE rn <= ${NAME_LIMIT}`,
      args: unique,
    }),
  ]);

  for (const row of countRs?.rows || []) {
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
    const guild = q.guild_id ?? q.guildId ?? "";
    if (typeof guild !== "string" || (guild && !/^\d{1,20}$/.test(guild))) {
      return send(res, 400, { error: "Invalid guild_id" }, "no-store");
    }
    // Single-card detail for deep links: /api/giveaways?id=gw_xxx
    if (q.id !== undefined) {
      const id = q.id;
      if (typeof id !== "string" || !/^gw_[A-Za-z0-9_-]+$/.test(id)) {
        return send(res, 400, { error: "Invalid giveaway id" }, "no-store");
      }
      const rs = await db().execute({ sql: ONE_SQL, args: [id] });
      const gw = rs.rows?.[0];
      if (!gw) return send(res, 404, { error: "Giveaway not found" }, "no-store");
      const hydrated = await hydrateMany([gw]);
      const { n, names } = hydrated.get(String(gw.id));
      return send(res, 200, { now, giveaway: card(gw, n, names, now) });
    }

    const prevLimit = Math.max(1, Math.min(10, Number(q.previous_limit ?? q.previousLimit ?? 5) || 5));
    const [liveRs, prevRs] = await Promise.all([
      db().execute({ sql: LIVE_SQL, args: { guild, limit: 25 } }),
      db().execute({ sql: PREV_SQL, args: { guild, limit: prevLimit } }),
    ]);
    const rows = [...(liveRs.rows || []), ...(prevRs.rows || [])];
    const hydrated = await hydrateMany(rows);
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
    console.error("Giveaways API error:", err);
    return send(res, 500, { error: "Internal server error" }, "no-store");
  }
}
