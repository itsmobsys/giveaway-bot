import { cors, db, send } from "./_lib/turso.js";

export default async function handler(req, res) {
  cors(res);
  if (req.method === "OPTIONS") return res.status(204).end();
  if (req.method !== "GET" && req.method !== "HEAD") {
    return send(res, 405, { ok: false, error: "GET only" }, "no-store");
  }
  try {
    await db().execute("SELECT 1");
    return send(res, 200, { ok: true, now: Date.now() });
  } catch (err) {
    console.error("Health API error:", err);
    return send(res, 500, { ok: false, error: "Internal server error" }, "no-store");
  }
}
