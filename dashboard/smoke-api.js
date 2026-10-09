// Offline endpoint regressions using libSQL's in-memory test database.
import assert from "node:assert/strict";
import admin from "./api/admin.js";
import giveaways from "./api/giveaways.js";
import health from "./api/health.js";
import { db } from "./api/_lib/turso.js";

const saved = {
  password: process.env.ADMIN_PASSWORD,
  url: process.env.TURSO_DATABASE_URL,
  token: process.env.TURSO_AUTH_TOKEN,
};
const restore = (key, value) => {
  if (value === undefined) delete process.env[key];
  else process.env[key] = value;
};

async function request(handler, method = "GET", body = {}, query = {}) {
  const res = {
    headers: {}, statusCode: 0, body: null,
    setHeader(key, value) { this.headers[key.toLowerCase()] = value; },
    status(code) { this.statusCode = code; return this; },
    json(value) { this.body = value; return this; },
    end() { return this; },
  };
  await handler({ method, body, query, headers: {} }, res);
  return res;
}
const post = (body) => request(admin, "POST", { password: "smoke-test-only", ...body });

try {
  delete process.env.ADMIN_PASSWORD;
  delete process.env.TURSO_DATABASE_URL;
  delete process.env.TURSO_AUTH_TOKEN;
  for (const method of ["POST", "OPTIONS", "GET"]) {
    const disabled = await request(admin, method, { action: "ping" });
    assert.equal(disabled.statusCode, 503, "admin is disabled without a password");
    assert.deepEqual(disabled.body, { error: "Admin disabled: ADMIN_PASSWORD not set" });
  }
  process.env.ADMIN_PASSWORD = "smoke-test-only";
  assert.equal((await post({ action: "ping" })).statusCode, 200);
  const wrong = await request(admin, "POST", { action: "ping", password: "x".repeat(100) });
  assert.equal(wrong.statusCode, 401, "different password lengths reject safely");
  assert.equal(wrong.body.error, "Wrong password");
  console.log("admin authentication/disabled ok");

  // Missing database configuration and internal errors never disclose details to callers.
  const originalError = console.error;
  const logged = [];
  console.error = (...args) => logged.push(args);
  try {
    for (const [handler, method, body] of [
      [admin, "POST", { action: "list", password: "smoke-test-only" }],
      [giveaways, "GET", {}],
      [health, "GET", {}],
    ]) {
      const response = await request(handler, method, body);
      assert.equal(response.statusCode, 500);
      assert.equal(response.body.error, "Internal server error");
    }
    assert.equal(logged.length, 3, "internal failures are logged server-side");
  } finally {
    console.error = originalError;
  }
  console.log("generic API errors ok");

  process.env.TURSO_DATABASE_URL = "file::memory:";
  const client = db();
  await client.execute(
    "CREATE TABLE simple_giveaways (id TEXT PRIMARY KEY, guild_id TEXT, prize TEXT, " +
    "winner_count INTEGER, created_at INTEGER, ends_at INTEGER, ended_at INTEGER, " +
    "entrant_count INTEGER, status TEXT, image_url TEXT, host_name TEXT, " +
    "claim_timeout_seconds INTEGER NOT NULL DEFAULT 0)"
  );
  await client.execute(
    "CREATE TABLE simple_entries (giveaway_id TEXT, username TEXT, entered_at INTEGER)"
  );
  await client.execute(
    "CREATE TABLE simple_claims (giveaway_id TEXT NOT NULL, user_id TEXT NOT NULL, " +
    "round INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'pending', " +
    "deadline_ms INTEGER NOT NULL DEFAULT 0, claimed_at INTEGER, skipped_by TEXT, " +
    "skipped_at INTEGER, created_at INTEGER NOT NULL DEFAULT 0, " +
    "PRIMARY KEY (giveaway_id, user_id, round))"
  );
  const now = Date.now();
  const rows = [
    ["gw_active", "active", now + 60000, null, 999],
    ["gw_stored", "ended", now - 2000, now - 2000, 12],
    ["gw_empty", "cancelled", now - 2500, now - 2500, 0],
    ["gw_fallback", "ended", now - 3000, now - 3000, null],
    ["gw_legacy", "ended", now - 4000, null, null],
    ["gw_tiea", "ended", now - 5000, now - 5000, null],
    ["gw_tieb", "ended", now - 5000, now - 5000, null],
  ];
  for (const [id, status, created, ended, entrantCount] of rows) {
    await client.execute({
      sql: "INSERT INTO simple_giveaways (id,guild_id,prize,winner_count,created_at,ends_at,ended_at,entrant_count,status) VALUES (?,?,?,?,?,?,?,?,?)",
      args: [id, "12345678901234567890", id, 2, created, created + 60000, ended, entrantCount, status],
    });
  }
  for (const [id, name] of [
    ["gw_active", "Active1"], ["gw_active", "Active2"],
    ["gw_empty", "Stale1"], ["gw_empty", "Stale2"],
    ["gw_fallback", "Old1"], ["gw_fallback", "Old2"], ["gw_fallback", "Old3"],
  ]) {
    await client.execute({ sql: "INSERT INTO simple_entries VALUES (?,?,?)", args: [id, name, now] });
  }
  for (const query of [
    { guild_id: "garbage" }, { guild_id: "1".repeat(21) }, { guild_id: "123", id: "../gw_active" },
    { id: "" }, { id: "gw_x;DROP" }, { id: ["gw_active"] }, { guild_id: ["123"] },
  ]) {
    assert.equal((await request(giveaways, "GET", {}, query)).statusCode, 400, JSON.stringify(query));
  }
  const publicList = await request(giveaways, "GET", {}, { guild_id: "12345678901234567890", previous_limit: 10 });
  assert.equal(publicList.statusCode, 200);
  assert.equal(publicList.body.live[0].entrants.count, 2, "live always uses current entries");
  assert.equal(publicList.body.previous.find((x) => x.id === "gw_stored").entrants.count, 12);
  assert.equal(publicList.body.previous.find((x) => x.id === "gw_empty").entrants.count, 0);
  assert.equal(publicList.body.previous.find((x) => x.id === "gw_fallback").entrants.count, 3);
  assert.equal(publicList.body.previous.find((x) => x.id === "gw_legacy").timer.ended_at, now - 4000);
  assert.deepEqual(publicList.body.previous.slice(-2).map((x) => x.id), ["gw_tieb", "gw_tiea"]);
  assert.equal((await request(giveaways, "GET", {}, { id: "gw_legacy" })).body.giveaway.timer.ended_at, now - 4000);
  console.log("giveaway filters, legacy ordering, archived counts ok");

  const listed = await post({ action: "list", limit: 2.9, offset: 1.9 });
  assert.equal(listed.statusCode, 200);
  assert.equal(listed.body.previous.length, 2);
  assert.deepEqual(listed.body.previous.map((x) => x.id), ["gw_empty", "gw_fallback"]);
  assert.equal(listed.body.previous[0].entrants.count, 0);
  assert.equal(listed.body.total, 6);
  assert.equal(listed.body.live, 1);
  assert.equal((await post({ action: "list", limit: Infinity, offset: Infinity })).body.previous.length, 6);
  assert.equal((await post({ action: "list", limit: 2, offset: 1e100 })).body.previous.length, 0);
  assert.equal((await post({ action: "list", limit: -10, offset: -50 })).body.previous[0].id, "gw_stored");
  assert.equal((await post({ action: "list", limit: -10, offset: -50 })).body.previous.length, 1);
  const active = await post({ action: "delete", ids: ["gw_active"] });
  assert.equal(active.statusCode, 409);
  assert.equal(active.body.deleted, 0);
  const bulk = await post({ action: "delete", ids: ["gw_fallback", "gw_active"] });
  assert.equal(bulk.statusCode, 200);
  assert.equal(bulk.body.deleted, 1);
  assert.equal(bulk.body.failed[0].id, "gw_active");
  assert.equal((await client.execute("SELECT COUNT(*) AS n FROM simple_entries WHERE giveaway_id='gw_active'")).rows[0].n, 2);
  assert.equal((await client.execute("SELECT COUNT(*) AS n FROM simple_entries WHERE giveaway_id='gw_fallback'")).rows[0].n, 0);
  assert.equal((await client.execute("SELECT COUNT(*) AS n FROM simple_giveaways WHERE id='gw_fallback'")).rows[0].n, 0);
  console.log("admin paging and transactional guarded deletion ok");
  client.close();
} finally {
  restore("ADMIN_PASSWORD", saved.password);
  restore("TURSO_DATABASE_URL", saved.url);
  restore("TURSO_AUTH_TOKEN", saved.token);
}
