import { createClient } from "@libsql/client/http";

let client = null;

/** `file:` URLs are the offline test database, never a production Turso URL. */
function isLocal(url) {
  return /^file:/i.test(url);
}

/**
 * A client-shaped proxy that resolves the real client on first use.
 *
 * The Node entry of `@libsql/client` pulls in the 8.9 MB native addon, which an
 * `libsql://` production URL never touches — import time is ~30 ms faster
 * without it. Local `file:` URLs (the smoke tests) still need the addon, so it
 * is loaded lazily and only for them. db() stays synchronous for every caller:
 * the proxy is returned immediately and its methods await the import.
 */
function lazyClient(load) {
  let real = null;
  const pending = load();
  // Attach a no-op rejection handler so a failed import is only reported to the
  // caller that awaits a method, not as an unhandled rejection.
  pending.then((c) => { real = c; }, () => {});
  const call = (method) => (...args) =>
    real ? Promise.resolve(real[method](...args)) : pending.then((c) => c[method](...args));
  return new Proxy({}, {
    get(_target, prop) {
      // Never awaitable: db() is documented and used as synchronous.
      if (prop === "then") return undefined;
      // Inspection/iteration hooks belong to the proxy, not the real client.
      if (typeof prop === "symbol") return undefined;
      return call(String(prop));
    },
  });
}

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
    if (isLocal(url)) {
      client = lazyClient(() =>
        import("@libsql/client").then(({ createClient: createLocal }) =>
          createLocal({ url, authToken: authToken || undefined })
        )
      );
    } else {
      client = createClient({ url, authToken: authToken || undefined });
    }
  }
  return client;
}

export function cors(res) {
  res.setHeader("Access-Control-Allow-Origin", "*");
  res.setHeader("Access-Control-Allow-Methods", "GET,OPTIONS");
  res.setHeader("Access-Control-Allow-Headers", "Content-Type");
}

export function send(res, status, body, cache = "s-maxage=30, stale-while-revalidate=30") {
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
 * Claim window (optional) rides along as `claim` so the wall can badge
 * giveaways that need a winner to press Claim Prize. Missing column (legacy
 * rows) reads as disabled.
 */
export function card(gw, entrantCount, usernames, now) {
  const winners = Math.max(1, Number(gw.winner_count) || 1);
  const isLive = gw.status === "active";
  const entrants = Math.max(0, Number(isLive || gw.entrant_count == null
    ? entrantCount : gw.entrant_count) || 0);
  const endsAt = Number(gw.ends_at) || 0;
  const msRemaining = isLive ? Math.max(0, endsAt - now) : 0;
  const percent = entrants > 0 ? Math.min(100, (winners / entrants) * 100) : 0;
  const claimTimeout = Number(gw.claim_timeout_seconds) || 0;
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
    claim: {
      enabled: claimTimeout > 0,
      timeout_seconds: claimTimeout,
    },
  };
}
