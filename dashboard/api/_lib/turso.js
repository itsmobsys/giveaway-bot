import { createClient } from "@libsql/client";

let client = null;

/** Shared Turso client (HTTP, safe for Vercel serverless). */
export function db() {
  const url = process.env.TURSO_DATABASE_URL;
  const authToken = process.env.TURSO_AUTH_TOKEN;
  if (!url) {
    const err = new Error(
      "TURSO_DATABASE_URL is not set. Point it at the same Turso DB as the bot."
    );
    err.statusCode = 500;
    throw err;
  }
  if (!client) {
    client = createClient({ url, authToken: authToken || undefined });
  }
  return client;
}

export function cors(res) {
  res.setHeader("Access-Control-Allow-Origin", "*");
  res.setHeader("Access-Control-Allow-Methods", "GET,OPTIONS");
  res.setHeader("Access-Control-Allow-Headers", "Content-Type");
}

export function send(res, status, body, cache = "s-maxage=10, stale-while-revalidate=20") {
  res.setHeader("Content-Type", "application/json");
  if (cache) res.setHeader("Cache-Control", cache);
  res.status(status).json(body);
}

/**
 * "?, ?, ?" for n bound parameters.
 *
 * Used to turn a list of ids into one IN (...) query instead of one query per
 * id. The result is only ever a list of placeholders: the ids themselves are
 * still passed as bound arguments, so nothing here reaches SQL as text.
 */
export function placeholders(n) {
  const count = Number.isInteger(n) && n > 0 ? n : 0;
  return Array.from({ length: count }, () => "?").join(",");
}

/**
 * Shape ONE dashboard card. Only the 4 agreed fields (+ ids for keying):
 *  - prize: what you are getting
 *  - entrants: people who joined (count + usernames, never user ids)
 *  - chance: chance of winning derived from winner_count / entrant_count
 *  - timer: ends_at + ms remaining (live) / ended_at (previous)
 */
export function card(gw, entrantCount, usernames, now) {
  const winners = Math.max(1, Number(gw.winner_count) || 1);
  const isLive = gw.status === "active";
  const entrants = Math.max(0, Number(isLive || gw.entrant_count == null
    ? entrantCount : gw.entrant_count) || 0);
  const endsAt = Number(gw.ends_at) || 0;
  const msRemaining = isLive ? Math.max(0, endsAt - now) : 0;
  const percent = entrants > 0 ? Math.min(100, (winners / entrants) * 100) : 0;
  return {
    id: gw.id,
    status: gw.status,
    prize: gw.prize,
    image_url: gw.image_url || null,
    host_name: gw.host_name || null,
    entrants: { count: entrants, usernames },
    chance: {
      winners,
      entrants,
      percent: Math.round(percent * 100) / 100,
      one_in: entrants > 0 ? Math.max(1, Math.round((entrants / winners) * 10) / 10) : null,
      text:
        entrants > 0
          ? `${winners} winner${winners === 1 ? "" : "s"} / ${entrants} entrant${entrants === 1 ? "" : "s"}`
          : "No entries yet",
    },
    timer: {
      ends_at: endsAt,
      ended_at: gw.ended_at != null ? Number(gw.ended_at) : null,
      ms_remaining: msRemaining,
      seconds_remaining: Math.floor(msRemaining / 1000),
      is_live: isLive,
    },
  };
}
