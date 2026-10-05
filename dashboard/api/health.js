import { cors, db, send } from "./_lib/turso.js";

export default async function handler(req, res) {
  cors(res);
  if (req.method === "OPTIONS") return res.status(204).end();
  try {
    await db().execute("SELECT 1");
    return send(res, 200, { ok: true, now: Date.now() });
  } catch (err) {
    return send(res, 500, { ok: false, error: err.message || "DB error" }, "no-store");
  }
}
