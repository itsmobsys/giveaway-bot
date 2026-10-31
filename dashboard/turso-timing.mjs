// Turso request timing probe (redacted — never prints secrets).
// Run: node turso-timing.mjs  (from GW_BOT/dashboard/)
// Reads ../.env for TURSO_DATABASE_URL / TURSO_AUTH_TOKEN, times stages.
import fs from "node:fs";
import path from "node:path";
import { performance } from "node:perf_hooks";

function loadEnv() {
  const p = path.resolve(process.cwd(), "../.env");
  const alt = path.resolve(process.cwd(), "../../GW_BOT/.env");
  const file = fs.existsSync(p) ? p : alt;
  if (!fs.existsSync(file)) return { file: "(none)", loaded: 0 };
  const text = fs.readFileSync(file, "utf8");
  let n = 0;
  for (const line of text.split(/\r?\n/)) {
    const m = line.match(/^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$/);
    if (!m) continue;
    const [, k, v] = m;
    if (process.env[k] === undefined) {
      let val = v;
      if ((val.startsWith('"') && val.endsWith('"')) || (val.startsWith("'") && val.endsWith("'"))) val = val.slice(1, -1);
      process.env[k] = val;
      n++;
    }
  }
  return { file, loaded: n };
}
function redactUrl(url) {
  try {
    const u = new URL(url);
    return u.protocol + "//" + u.host + " (path/query hidden)";
  } catch { return "(unparseable scheme, len=" + String(url || "").length + ")"; }
}
function redactErr(e, url, token) {
  let s = String(e && e.message ? e.message : e);
  if (url && s.includes(url)) s = s.split(url).join("[TURSO_URL]");
  if (token) {
    // tokens are long; also redact any 64+ char alnum chunk that could be a fragment
    s = s.split(token).join("[TOKEN]");
    s = s.replace(/[A-Za-z0-9_\-]{64,}/g, "[TOKEN-FRAG]");
  }
  // hostname fragment is not secret, keep it
  return s.slice(0, 500);
}
const t0 = performance.now();
const envInfo = loadEnv();
const tEnv = performance.now();
const url = process.env.TURSO_DATABASE_URL || "";
const token = process.env.TURSO_AUTH_TOKEN || "";
console.log("env file: " + envInfo.file + " (keys loaded: " + envInfo.loaded + ")");
console.log("TURSO_DATABASE_URL: " + (url ? redactUrl(url) : "(missing)"));
console.log("TURSO_DATABASE_URL len: " + url.length + ", TURSO_AUTH_TOKEN len: " + token.length);
console.log("node: " + process.version);
if (!url) { console.log("ABORT: TURSO_DATABASE_URL missing"); process.exit(2); }
const { createClient } = await import("@libsql/client");
const tImport = performance.now();
const tMk0 = performance.now();
const client = createClient({ url, authToken: token || undefined });
const tMk = performance.now();
async function timed(label, fn) {
  const s = performance.now();
  try {
    const r = await fn();
    const ms = performance.now() - s;
    console.log(label.padEnd(34) + ms.toFixed(1).padStart(9) + " ms");
    return { ms, r };
  } catch (e) {
    const ms = performance.now() - s;
    console.log(label.padEnd(34) + ms.toFixed(1).padStart(9) + " ms  ERROR: " + redactErr(e, url, token));
    return { ms, err: e };
  }
}
console.log("--- stages (ms) ---");
console.log(("env load").padEnd(34) + (tEnv - t0).toFixed(1).padStart(9) + " ms");
console.log(("import @libsql/client").padEnd(34) + (tImport - tEnv).toFixed(1).padStart(9) + " ms");
console.log(("createClient (no I/O)").padEnd(34) + (tMk - tMk0).toFixed(1).padStart(9) + " ms");
// 1. cold: first round-trip includes TLS + auth + query
const cold = await timed("cold SELECT 1 (handshake+query)", () => client.execute("SELECT 1"));
if (cold.err) { console.log("ABORT: cold query failed, not running warm set"); process.exit(3); }
// 2. warm repeats
for (let i = 1; i <= 5; i++) await timed("warm SELECT 1 #" + i, () => client.execute("SELECT 1"));
// 3. app-shaped queries (same as api/giveaways.js)
await timed("LIVE_SQL (25 active)", () => client.execute({ sql: "SELECT id, guild_id, prize, winner_count, ends_at, COALESCE(ended_at, created_at) AS ended_at, entrant_count, status, image_url, host_name, claim_timeout_seconds FROM simple_giveaways WHERE status = 'active' ORDER BY ends_at ASC LIMIT 25", args: [] }));
const idsRes = await timed("collect ids (LIMIT 50)", async () => client.execute("SELECT id FROM simple_giveaways LIMIT 50"));
let ids = [];
try { ids = (idsRes.r?.rows || []).map(r => String(r.id)).slice(0, 20); } catch {}
console.log("sampled ids for hydrate: " + ids.length);
if (ids.length) {
  const list = ids.map(() => "?").join(",");
  await timed("hydrate COUNT GROUP BY (" + ids.length + " ids)", () => client.execute({ sql: "SELECT giveaway_id, COUNT(*) AS n FROM simple_entries WHERE giveaway_id IN (" + list + ") GROUP BY giveaway_id", args: ids }));
  await timed("hydrate names ROW_NUMBER (" + ids.length + " ids)", () => client.execute({ sql: "SELECT giveaway_id, username FROM (SELECT giveaway_id, username, ROW_NUMBER() OVER (PARTITION BY giveaway_id ORDER BY entered_at ASC) AS rn FROM simple_entries WHERE giveaway_id IN (" + list + ")) WHERE rn <= 100", args: ids }));
}
await timed("simple_claims probe (LIMIT 1)", () => client.execute("SELECT giveaway_id FROM simple_claims LIMIT 1"));
const total = performance.now() - t0;
console.log("---");
console.log("total script time:".padEnd(34) + total.toFixed(1).padStart(9) + " ms");
console.log("NOTE: cold>>warm => connection/TLS/auth dominates; all-slow => network distance/region; hydrate>>single => data transfer.");
