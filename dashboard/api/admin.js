import { timingSafeEqual } from "node:crypto";
import { db, send } from "./_lib/turso.js";

// Default password so the panel works with zero extra setup; override with
// the ADMIN_PASSWORD env var. Note: anyone who can read this repo (or its
// Vercel env settings) can see it — it keeps curious visitors out, nothing more.
const PASSWORD = process.env.ADMIN_PASSWORD || "duggalbadmoshnahirahalol";

function authorized(pw) {
  const a = Buffer.from(String(pw ?? ""));
  const b = Buffer.from(PASSWORD);
  return a.length === b.length && timingSafeEqual(a, b);
}

export default async function handler(req, res) {
  if (req.method === "OPTIONS") return res.status(204).end();
  if (req.method !== "POST") return send(res, 405, { error: "POST only" }, "no-store");

  let body = req.body;
  if (typeof body === "string") {
    try {
      body = JSON.parse(body);
    } catch {
      body = {};
    }
  }
  const pw = req.headers?.["x-admin-password"] ?? body?.password;
  if (!authorized(pw)) return send(res, 401, { error: "Wrong password" }, "no-store");

  const { action } = body || {};
  if (action === "ping") return send(res, 200, { ok: true }, "no-store");

  if (action === "delete") {
    const id = String(body?.id || "");
    if (!id) return send(res, 400, { error: "Missing giveaway id" }, "no-store");
    try {
      const rs = await db().execute({
        sql: "SELECT id, status, prize FROM simple_giveaways WHERE id = ? LIMIT 1",
        args: [id],
      });
      const gw = rs.rows?.[0];
      if (!gw) return send(res, 404, { error: "Giveaway not found" }, "no-store");
      if (gw.status === "active") {
        return send(
          res, 409,
          { error: "That giveaway is still live — end it in Discord first." },
          "no-store",
        );
      }
      await db().execute({ sql: "DELETE FROM simple_entries WHERE giveaway_id = ?", args: [id] });
      await db().execute({ sql: "DELETE FROM simple_giveaways WHERE id = ?", args: [id] });
      return send(res, 200, { deleted: id, prize: gw.prize }, "no-store");
    } catch (err) {
      return send(res, err.statusCode || 500, { error: err.message || "DB error" }, "no-store");
    }
  }

  return send(res, 400, { error: "Unknown action" }, "no-store");
}
