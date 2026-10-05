import { readFile } from "node:fs/promises";
import { join } from "node:path";

/**
 * Serves the dashboard page shell through a function instead of relying on
 * Vercel static output. The /api/* functions demonstrably deploy and run, so
 * the page rides that same pipeline: vercel.json rewrites /, /styles.css,
 * /app.js and /format.js here with ?f=<file>.
 *
 * Only these 4 files are servable — the allowlist makes path traversal
 * impossible even before normalization.
 */
const MIME = {
  ".html": "text/html; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
};

const FILES = {
  "index.html": { mime: MIME[".html"], cache: "public, s-maxage=30, stale-while-revalidate=60" },
  "styles.css": { mime: MIME[".css"], cache: "public, s-maxage=86400, immutable" },
  "app.js": { mime: MIME[".js"], cache: "public, s-maxage=86400, immutable" },
  "format.js": { mime: MIME[".js"], cache: "public, s-maxage=86400, immutable" },
};

export function resolveFile(query) {
  const f = typeof query?.f === "string" ? query.f : "";
  if (Object.hasOwn(FILES, f)) return f;
  return null;
}

export default async function handler(req, res) {
  const file = resolveFile(req.query);
  if (!file) {
    res.status(404).send("not found");
    return;
  }
  try {
    // includeFiles: ["public/**"] in vercel.json bundles this dir with us.
    const body = await readFile(join(process.cwd(), "public", file));
    res.setHeader("Content-Type", FILES[file].mime);
    res.setHeader("Cache-Control", FILES[file].cache);
    res.status(200).send(body);
  } catch {
    res.status(404).send("not found");
  }
}
