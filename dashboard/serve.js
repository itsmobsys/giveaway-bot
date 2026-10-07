// Serves public/ and mocks /api/giveaways with sample data, so the page can be
// opened in a browser before Vercel exists. Run: node serve.js
import { createHash, timingSafeEqual } from "node:crypto";
import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { extname, join } from "node:path";

const PUB = new URL("./public/", import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1");
const TYPES = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css" };
const DEV_PASSWORD = process.env.ADMIN_PASSWORD || "local-preview-only";

const now = Date.now();
const sample = {
  now,
  live: [
    {
      id: "gw_live1", status: "active", prize: "$20 Steam Gift Card", image_url: null, host_name: "ModPete",
      entrants: { count: 14, usernames: ["Ann", "Zed", "Bo", "Cy", "Dee", "Eli", "Fay", "Gus", "Hal", "Ivy", "Joe", "Kim", "Lia", "Max"] },
      chance: { winners: 1, entrants: 14, percent: 7.14, one_in: 14, text: "1 winner / 14 entrants" },
      timer: { ends_at: now + 2 * 3600_000 + 11 * 60_000 + 5000, ended_at: null, ms_remaining: 2 * 3600_000 + 11 * 60_000 + 5000, seconds_remaining: 8465, is_live: true },
    },
  ],
  previous: [
    {
      id: "gw_prev1", status: "ended", prize: "Nitro Classic 1 month", image_url: null, host_name: null,
      entrants: { count: 0, usernames: [] },
      chance: { winners: 1, entrants: 0, percent: 0, one_in: null, text: "No entries yet" },
      timer: { ends_at: now - 7200_000, ended_at: now - 7200_000, ms_remaining: 0, seconds_remaining: 0, is_live: false },
    },
    {
      id: "gw_prev2", status: "cancelled", prize: "Void raffle", image_url: null, host_name: null,
      entrants: { count: 3, usernames: ["Ann", "Zed", "Bo"] },
      chance: { winners: 1, entrants: 3, percent: 33.3, one_in: 3, text: "1 winner / 3 entrants" },
      timer: { ends_at: now - 90_000_000, ended_at: now - 90_000_000, ms_remaining: 0, seconds_remaining: 0, is_live: false },
    },
    {
      id: "gw_prev3", status: "ended", prize: "₹150 Steam voucher", image_url: "https://picsum.photos/id/180/540/460", host_name: "ModPete",
      entrants: { count: 27, usernames: [] },
      chance: { winners: 2, entrants: 27, percent: 7.41, one_in: 13.5, text: "2 winners / 27 entrants" },
      timer: { ends_at: now - 260_000_000, ended_at: now - 260_000_000, ms_remaining: 0, seconds_remaining: 0, is_live: false },
    },
    {
      id: "gw_prev4", status: "cancelled", prize: "Give up your data", image_url: null, host_name: "SysAdmin",
      entrants: { count: 0, usernames: [] },
      chance: { winners: 1, entrants: 0, percent: 0, one_in: null, text: "No entries yet" },
      timer: { ends_at: now - 400_000_000, ended_at: now - 400_000_000, ms_remaining: 0, seconds_remaining: 0, is_live: false },
    },
  ],
};

createServer(async (req, res) => {
  const path = new URL(req.url, "http://x").pathname;
  if (path === "/api/giveaways") {
    res.writeHead(200, { "Content-Type": "application/json" });
    return res.end(JSON.stringify(sample));
  }

  // The admin panel needs a POST mock, or it can't be previewed locally.
  if (path === "/api/admin") {
    let raw = "";
    for await (const chunk of req) raw += chunk;
    let body = {};
    try {
      body = JSON.parse(raw || "{}");
    } catch {}
    const json = (code, obj) => {
      res.writeHead(code, { "Content-Type": "application/json" });
      res.end(JSON.stringify(obj));
    };
    const supplied = createHash("sha256").update(String(body.password ?? "")).digest();
    const expected = createHash("sha256").update(DEV_PASSWORD).digest();
    if (!timingSafeEqual(supplied, expected)) {
      await new Promise((resolve) => setTimeout(resolve, 300));
      return json(401, { error: "Wrong password" });
    }
    if (body.action === "list") {
      const offset = Number(body.offset) || 0;
      const limit = Number(body.limit) || 25;
      return json(200, {
        now,
        live: sample.live.length,
        total: sample.previous.length,
        previous: sample.previous.slice(offset, offset + limit),
      });
    }
    if (body.action === "delete") {
      const ids = new Set(Array.isArray(body.ids) ? body.ids : [body.id]);
      const gone = sample.previous.filter((g) => ids.has(g.id)).length;
      sample.previous = sample.previous.filter((g) => !ids.has(g.id));
      return json(200, { deleted: gone, failed: [] });
    }
    return json(200, { ok: true });
  }

  const file = path === "/" ? "index.html" : path === "/admin" ? "admin.html" : path.slice(1);
  try {
    const body = await readFile(join(PUB, file));
    res.writeHead(200, { "Content-Type": TYPES[extname(file)] ?? "application/octet-stream" });
    res.end(body);
  } catch {
    res.writeHead(404).end("not found");
  }
}).listen(4321, () => console.log("http://localhost:4321"));