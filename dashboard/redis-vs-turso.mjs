// redis-vs-turso.mjs - same-machine, same-process, interleaved benchmark of Turso
// (libSQL over the network) vs Redis (RESP over TCP) for the shapes this bot issues.
//
// Redacted by design: .env is parsed locally with a tiny hand-rolled parser and only
// scheme checks, row counts and timings reach stdout - never a host, port or token.
// Turso is strictly READ-ONLY (every statement passes the SELECT guard below), so the
// write leg has no Turso number and says so instead of inventing one.
//
// Why a hand-rolled RESP client: dashboard/node_modules has no redis/ioredis and this
// probe must not add dependencies, so it speaks the wire protocol over node:net.
//
// Run: node redis-vs-turso.mjs   (from GW_BOT/dashboard/)

import fs from "node:fs";
import net from "node:net";
import tls from "node:tls";
import path from "node:path";
import { randomUUID } from "node:crypto";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const N = Number(process.env.BENCH_SAMPLES || 30);
const OP_TIMEOUT_MS = 20000;
// Every key this probe writes lives under this namespace and carries a TTL, so even a
// hard crash cannot leak keys permanently.
const PREFIX = "gwbot:v1:bench:" + randomUUID() + ":";
const READ_KEY = PREFIX + "read:blob";
const WRITE_KEY = PREFIX + "write:blob";
const READ_SQL = "SELECT * FROM simple_giveaways WHERE status = 'active' ORDER BY ends_at ASC LIMIT 5";
const CAVEAT = "CAVEAT: this PC is in India while the bot runs on Render Singapore, so every number is an UPPER BOUND.";

const hr = () => process.hrtime.bigint();
const msSince = (t) => Number(process.hrtime.bigint() - t) / 1e6;
const fmt = (v) => v.toFixed(3);

// ---------------------------------------------------------------- env (no values out)
function loadEnv() {
  const candidates = [
    path.resolve(HERE, "../.env"),
    path.resolve(HERE, ".env"),
    path.resolve(process.cwd(), ".env"),
    path.resolve(process.cwd(), "../.env"),
  ];
  for (const file of candidates) {
    if (!fs.existsSync(file)) continue;
    const env = {};
    for (const line of fs.readFileSync(file, "utf8").split(/\r?\n/)) {
      const m = line.match(/^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$/);
      if (!m) continue;
      let v = m[2];
      const q = v.charAt(0);
      if (v.length >= 2 && (q === '"' || q === "'") && v.endsWith(q)) v = v.slice(1, -1);
      env[m[1]] = v;
    }
    return { env, file: "found" };
  }
  return { env: {}, file: "missing" };
}

// ---------------------------------------------------------------- error hygiene
// Only a class/code ever reaches stdout: messages can echo hosts, ports and URLs.
class TimeoutError extends Error {
  constructor() {
    super("operation timed out");
    this.name = "TimeoutError";
    this.code = "ETIMEDOUT";
  }
}
function errClass(e) {
  if (!e) return "UnknownError";
  const code = typeof e.code === "string" && /^[A-Za-z0-9_]{2,32}$/.test(e.code) ? e.code : null;
  const name = (e.constructor && e.constructor.name) || "Error";
  return code || name;
}
function withTimeout(promise, ms) {
  let timer = null;
  const guard = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new TimeoutError()), ms);
  });
  return Promise.race([promise, guard]).finally(() => clearTimeout(timer));
}

// ---------------------------------------------------------------- minimal RESP client
class RespError extends Error {
  // Keep only the leading uppercase token ("ERR", "WRONGTYPE", "NOAUTH"): a class,
  // never a key name or a connection detail.
  constructor(line) {
    const m = /^([A-Z][A-Z0-9_]{1,23})\b/.exec(String(line));
    super("redis error reply");
    this.name = "RespError";
    this.code = m ? m[1] : "RESP_ERROR";
  }
}

function encodeCmd(args) {
  const parts = [Buffer.from("*" + args.length + "\r\n", "utf8")];
  for (const a of args) {
    const b = Buffer.isBuffer(a) ? a : Buffer.from(String(a), "utf8");
    parts.push(Buffer.from("$" + b.length + "\r\n", "utf8"), b, Buffer.from("\r\n", "utf8"));
  }
  return Buffer.concat(parts);
}

class RespClient {
  constructor(rawUrl, opTimeoutMs = OP_TIMEOUT_MS) {
    const u = new URL(rawUrl);
    if (u.protocol !== "redis:" && u.protocol !== "rediss:") {
      throw Object.assign(new Error("unsupported redis scheme"), { code: "EBADSCHEME" });
    }
    this.tls = u.protocol === "rediss:";
    this.host = u.hostname;
    this.port = Number(u.port || 6379);
    this.user = u.username ? decodeURIComponent(u.username) : "";
    this.pass = u.password ? decodeURIComponent(u.password) : "";
    this.db = (u.pathname || "/0").replace(/^\//, "") || "0";
    this.opTimeoutMs = opTimeoutMs;
    this.buf = Buffer.alloc(0);
    this.queue = [];
    this.sock = null;
    this.ready = false;
    this.closed = false;
  }

  connect() {
    if (this.sock) return Promise.resolve();
    return withTimeout(
      new Promise((resolve, reject) => {
        const opts = { host: this.host, port: this.port };
        const sock = this.tls ? tls.connect({ ...opts, servername: this.host }) : net.connect(opts);
        sock.setNoDelay(true);
        sock.on("data", (c) => this._onData(c));
        sock.on("error", (e) => {
          this.closed = true;
          this._failAll(e);
          if (!this.ready) reject(e);
        });
        sock.on("close", () => {
          this.closed = true;
          const e = Object.assign(new Error("socket closed"), { code: "ECLOSED" });
          this._failAll(e);
          if (!this.ready) reject(e);
        });
        const onReady = () => {
          this.ready = true;
          this.sock = sock;
          resolve();
        };
        if (this.tls) sock.once("secureConnect", onReady);
        else sock.once("connect", onReady);
      }),
      this.opTimeoutMs,
    ).then(async () => {
      if (this.pass) {
        if (this.user && this.user !== "default") await this.send("AUTH", this.user, this.pass);
        else await this.send("AUTH", this.pass);
      }
      if (this.db && this.db !== "0") await this.send("SELECT", this.db);
    });
  }

  send(...args) {
    if (this.closed || !this.sock) {
      return Promise.reject(Object.assign(new Error("client not connected"), { code: "ECLOSED" }));
    }
    return withTimeout(
      new Promise((resolve, reject) => {
        this.queue.push({ resolve, reject });
        this.sock.write(encodeCmd(args));
      }),
      this.opTimeoutMs,
    ).catch((e) => {
      // A timed-out reply would desynchronise the FIFO, so poison the connection.
      if (e instanceof TimeoutError) {
        this._failAll(e);
        this.destroy();
      }
      throw e;
    });
  }

  destroy() {
    this.closed = true;
    if (this.sock) {
      try {
        this.sock.destroy();
      } catch {}
    }
  }

  _failAll(err) {
    const q = this.queue;
    this.queue = [];
    for (const p of q) p.reject(err);
  }

  _onData(chunk) {
    this.buf = Buffer.concat([this.buf, chunk]);
    for (;;) {
      const r = this._parse(this.buf);
      if (r === null) return;
      this.buf = r.rest;
      const p = this.queue.shift();
      if (!p) continue;
      if (r.error) p.reject(r.error);
      else p.resolve(r.value);
    }
  }

  // Returns {value|error, rest} for one complete reply, or null when more bytes are needed.
  _parse(buf) {
    if (buf.length < 4) return null;
    const type = String.fromCharCode(buf[0]);
    const nl = buf.indexOf("\r\n");
    if (nl === -1) return null;
    const head = buf.toString("utf8", 1, nl);
    const after = buf.subarray(nl + 2);
    if (type === "+") return { value: head, rest: after };
    if (type === "-") return { error: new RespError(head), rest: after };
    if (type === ":") return { value: Number(head), rest: after };
    if (type === "$") {
      const len = Number(head);
      if (len === -1) return { value: null, rest: after };
      if (after.length < len + 2) return null;
      return { value: after.toString("utf8", 0, len), rest: after.subarray(len + 2) };
    }
    if (type === "*") {
      const count = Number(head);
      if (count === -1) return { value: null, rest: after };
      const items = [];
      let rest = after;
      for (let i = 0; i < count; i++) {
        const r = this._parse(rest);
        if (r === null) return null;
        if (r.error) return { error: r.error, rest: r.rest };
        items.push(r.value);
        rest = r.rest;
      }
      return { value: items, rest };
    }
    return { error: new RespError("ERR unexpected-resp-type"), rest: after };
  }
}

// ---------------------------------------------------------------- stats helpers
function stats(times) {
  const s = [...times].sort((a, b) => a - b);
  const at = (p) => s[Math.min(s.length - 1, Math.max(0, Math.round((p / 100) * (s.length - 1))))];
  return { min: s[0], p50: at(50), p95: at(95), max: s[s.length - 1], n: s.length };
}
function printGroup(label, times, fallbackErr) {
  if (!times.length) {
    console.log(label + ": unreachable: " + errClass(fallbackErr));
    return;
  }
  const st = stats(times);
  console.log(label + ": " + fmt(st.min) + " .. " + fmt(st.p50) + " .. " + fmt(st.p95) + " .. " + fmt(st.max) + " ms (n=" + st.n + ")");
}
const p50 = (times) => (times.length ? fmt(stats(times).p50) : "unreachable: n/a");

// ---------------------------------------------------------------- representative blob
const BLOB = JSON.stringify(
  Array.from({ length: 5 }, (_, i) => ({
    id: "00000" + (i + 1) + "-bench-0000-0000-00000000000" + i,
    guild_id: "1000000000000000" + i,
    prize: "Nitro Classic (bench sample #" + (i + 1) + ")",
    ends_at: new Date(Date.now() + (i + 1) * 3600000).toISOString(),
    status: "active",
    winner_count: 1,
    entrant_count: 42 + i,
    host_name: "bench-host-" + i,
    image_url: null,
    claim_timeout_seconds: 86400,
  })),
);

// ---------------------------------------------------------------- main
async function main() {
  const { env } = loadEnv();
  const tursoUrl = env.TURSO_DATABASE_URL || "";
  const tursoToken = env.TURSO_AUTH_TOKEN || "";
  const redisUrl = env.REDIS_URL || "";

  console.log("redis-vs-turso benchmark - same machine, same process, interleaved");
  console.log("node " + process.version + "; samples per group n=" + N + "; timer=process.hrtime.bigint()");
  console.log("env: .env parsed locally; no host/port/token/value is printed by this script");
  console.log("env shapes: TURSO_DATABASE_URL=" + (tursoUrl.startsWith("libsql://") ? "libsql://..." : tursoUrl ? "(unexpected scheme)" : "(missing)") + "; REDIS_URL=" + (/^rediss?:/.test(redisUrl) ? redisUrl.split("://")[0] + "://..." : redisUrl ? "(unexpected scheme)" : "(missing)"));
  console.log("");

  let partial = false;

  // --- Turso client (import excluded from the cold number) ---
  let client = null;
  let tursoErr = null;
  if (!tursoUrl) {
    tursoErr = Object.assign(new Error("TURSO_DATABASE_URL missing"), { code: "ENOENV" });
  } else {
    const tImport = hr();
    const mod = await import("@libsql/client");
    const importMs = msSince(tImport);
    client = mod.createClient({ url: tursoUrl, authToken: tursoToken || undefined });
    console.log("libsql client import: " + fmt(importMs) + " ms (informational; excluded from every measurement)");
  }
  const query = (sql, args) => {
    if (!/^\s*(select|with)\b/i.test(sql)) {
      // Read-only guarantee: nothing but SELECT/WITH can ever leave this process.
      return Promise.reject(Object.assign(new Error("blocked non-SELECT statement"), { code: "EREADONLY" }));
    }
    return client.execute(args ? { sql, args } : sql);
  };

  // --- Redis client ---
  let redis = null;
  let redisErr = null;
  try {
    if (!redisUrl) throw Object.assign(new Error("REDIS_URL missing"), { code: "ENOENV" });
    redis = new RespClient(redisUrl);
  } catch (e) {
    redisErr = e;
  }

  let tursoAlive = !!client;
  let redisAlive = !!redis && !redisErr;

  // --- cold first round trip per side (before any warm-up) ---
  let coldTurso = null;
  let coldRedis = null;
  if (tursoAlive) {
    const t = hr();
    try {
      await withTimeout(query("SELECT 1"), OP_TIMEOUT_MS);
      coldTurso = msSince(t);
      console.log("cold turso: " + fmt(coldTurso) + " ms (first SELECT 1 incl. libsql connect/auth/TLS)");
    } catch (e) {
      tursoAlive = false;
      tursoErr = e;
      console.log("cold turso: unreachable: " + errClass(e));
    }
  } else {
    console.log("cold turso: unreachable: " + errClass(tursoErr));
  }
  if (redisAlive) {
    const t = hr();
    try {
      await redis.connect();
      await redis.send("PING");
      coldRedis = msSince(t);
      console.log("cold redis: " + fmt(coldRedis) + " ms (first connect + AUTH/SELECT handshake + PING)");
    } catch (e) {
      redisAlive = false;
      redisErr = e;
      console.log("cold redis: unreachable: " + errClass(e));
    }
  } else {
    console.log("cold redis: unreachable: " + errClass(redisErr));
  }
  console.log("");

  // --- warm-up: pay handshake/pool costs once, unmeasured, 2 rounds per side ---
  for (let i = 0; i < 2 && tursoAlive; i++) {
    try {
      await withTimeout(query("SELECT 1"), OP_TIMEOUT_MS);
    } catch (e) {
      tursoAlive = false;
      tursoErr = e;
    }
  }
  for (let i = 0; i < 2 && redisAlive; i++) {
    try {
      await redis.send("PING");
    } catch (e) {
      redisAlive = false;
      redisErr = e;
    }
  }
  // Seed the read key once (TTL-bounded), unmeasured.
  if (redisAlive) {
    try {
      await redis.send("SET", READ_KEY, BLOB, "PX", "120000");
    } catch (e) {
      redisAlive = false;
      redisErr = e;
    }
  }

  const rttT = [];
  const rttR = [];
  const readT = [];
  const readR = [];
  const writeR = [];

  const measure = async (bucket, fn) => {
    const t = hr();
    try {
      await withTimeout(fn(), OP_TIMEOUT_MS);
      bucket.push(msSince(t));
      return null;
    } catch (e) {
      return e;
    }
  };

  // Interleave sample by sample and alternate who goes first, so network drift and
  // any ordering bias hit both sides equally.
  const interleave = async (tursoOp, redisOp, outT, outR) => {
    for (let i = 0; i < N; i++) {
      const order = i % 2 === 0 ? [0, 1] : [1, 0];
      for (const which of order) {
        if (which === 0 && tursoAlive) {
          const err = await measure(outT, tursoOp);
          if (err) {
            tursoAlive = false;
            tursoErr = err;
            if (outT.length) partial = true;
          }
        } else if (which === 1 && redisAlive) {
          const err = await measure(outR, redisOp);
          if (err) {
            redisAlive = false;
            redisErr = err;
            if (outR.length) partial = true;
          }
        }
      }
    }
  };

  console.log("--- A. pure round trip (interleaved) ---");
  await interleave(() => query("SELECT 1"), () => redis.send("PING"), rttT, rttR);
  printGroup("rtt turso (SELECT 1)", rttT, tursoErr);
  printGroup("rtt redis (PING)", rttR, redisErr);
  console.log("");

  console.log("--- B. real read shape (interleaved) ---");
  console.log("read sql: " + READ_SQL);
  await interleave(() => query(READ_SQL), () => redis.send("GET", READ_KEY), readT, readR);
  printGroup("read turso (SELECT * ... LIMIT 5)", readT, tursoErr);
  printGroup("read redis (GET json blob)", readR, redisErr);

  // Honest payload-size comparison for the two read legs.
  let readRows = -1;
  let tursoBytes = -1;
  if (tursoAlive) {
    try {
      const res = await withTimeout(query(READ_SQL), OP_TIMEOUT_MS);
      const rows = (res.rows || []).map((r) => Object.fromEntries(Object.entries(r)));
      readRows = rows.length;
      tursoBytes = Buffer.byteLength(JSON.stringify(rows), "utf8");
    } catch (e) {
      tursoAlive = false;
      tursoErr = e;
    }
  }
  const redisBytes = Buffer.byteLength(BLOB, "utf8");
  if (tursoBytes >= 0) {
    const ratio = tursoBytes > 0 ? (redisBytes / tursoBytes).toFixed(2) + "x" : "n/a";
    console.log("read payload: turso rows=" + readRows + " bytes=" + tursoBytes + "; redis blob bytes=" + redisBytes + " (redis/turso=" + ratio + ")");
    if (readRows === 0) console.log("read payload note: simple_giveaways has no active rows, so the turso read leg transferred almost nothing");
    else if (tursoBytes > 0 && (redisBytes / tursoBytes > 2 || redisBytes / tursoBytes < 0.5)) console.log("read payload note: payload sizes differ by more than 2x; both legs are still RTT-dominated at this size");
  } else {
    console.log("read payload: redis blob bytes=" + redisBytes + "; turso payload unreachable: " + errClass(tursoErr));
  }
  console.log("");

  console.log("--- C. write shape (interleaved where measurable) ---");
  for (let i = 0; i < N && redisAlive; i++) {
    const err = await measure(writeR, () => redis.send("SET", WRITE_KEY, BLOB, "PX", "30000"));
    if (err) {
      redisAlive = false;
      redisErr = err;
      if (writeR.length) partial = true;
    }
  }
  printGroup("write redis (SET key blob PX 30000)", writeR, redisErr);
  console.log("Turso write not measured (read-only run)");
  console.log("");

  // --- cleanup: ONE DEL call, one retry, then an independent leftover scan ---
  let leftover = -1;
  let cleanupNote = "";
  const cleanupUrls = redisUrl ? [redisUrl] : [];
  for (let attempt = 0; attempt < 2 && leftover !== 0 && cleanupUrls.length; attempt++) {
    const c = new RespClient(redisUrl);
    try {
      await c.connect();
      await c.send("DEL", READ_KEY, WRITE_KEY);
      const check = (await c.send("MGET", READ_KEY, WRITE_KEY)) || [];
      let remaining = check.filter((v) => v !== null).length;
      try {
        let cursor = "0";
        for (let i = 0; i < 50; i++) {
          const [next, keys] = await c.send("SCAN", cursor, "MATCH", PREFIX + "*", "COUNT", "1000");
          cursor = String(next);
          remaining += (keys || []).length;
          if (cursor === "0") break;
        }
      } catch (e) {
        cleanupNote = " (SCAN verify skipped: " + errClass(e) + ")";
      }
      leftover = remaining;
      c.destroy();
      break;
    } catch (e) {
      c.destroy();
      leftover = -1;
      cleanupNote = " (cleanup attempt " + (attempt + 1) + " failed: " + errClass(e) + ")";
      partial = true;
    }
  }
  console.log("cleanup: DEL " + READ_KEY.slice(0, PREFIX.length) + "... in one call, retry once" + cleanupNote);
  console.log("leftover redis keys: " + (leftover < 0 ? "unknown" + cleanupNote : leftover));
  console.log("");

  // --- summary ---
  const status = !tursoAlive || !redisAlive || partial ? "PARTIAL" : "COMPLETE";
  console.log("SUMMARY");
  console.log("status: " + status + (tursoAlive ? "" : " (turso unreachable: " + errClass(tursoErr) + ")") + (redisAlive ? "" : " (redis unreachable: " + errClass(redisErr) + ")"));
  console.log("cold turso ms: " + (coldTurso === null ? "unreachable: " + errClass(tursoErr) : fmt(coldTurso)));
  console.log("cold redis ms: " + (coldRedis === null ? "unreachable: " + errClass(redisErr) : fmt(coldRedis)));
  console.log("warm rtt turso p50: " + p50(rttT));
  console.log("warm rtt redis p50: " + p50(rttR));
  console.log("read-shape turso p50: " + p50(readT));
  console.log("read-shape redis p50: " + p50(readR));
  console.log("redis SET p50: " + p50(writeR));
  console.log("leftover redis keys: " + (leftover < 0 ? "unknown" : leftover));
  console.log(CAVEAT);

  // exit code 0 even on a partial run: this probe reports, it does not fail builds
  process.exitCode = 0;
}

try {
  await main();
} catch (e) {
  console.log("probe aborted: " + errClass(e));
  process.exitCode = 0;
}
