/**
 * Script-side database access.
 *
 * Node scripts cannot import TypeScript directly, so this mirrors the small
 * surface of `src/lib/db.ts` that they need. Keeping it separate means the app
 * still gets proper types and tree-shaking.
 */

import { createClient } from "@libsql/client";
import { existsSync, mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";

let client = null;

function getClient() {
  if (client) return client;
  const url = process.env.TURSO_DATABASE_URL;
  const authToken = process.env.TURSO_AUTH_TOKEN;

  if (url && url.startsWith("libsql://")) {
    client = createClient({ url, authToken });
  } else {
    const path = resolve(process.env.SQLITE_PATH ?? "./data/giveaways.db");
    if (!existsSync(path)) mkdirSync(dirname(path), { recursive: true });
    client = createClient({ url: `file:${path}` });
  }
  return client;
}

export async function all(sql, params = []) {
  const result = await getClient().execute({ sql, args: params });
  return result.rows;
}

export async function first(sql, params = []) {
  const rows = await all(sql, params);
  return rows.length > 0 ? rows[0] : null;
}

export async function executeScript(statements) {
  if (statements.length === 0) return;
  await getClient().batch(
    statements.map((sql) => ({ sql, args: [] })),
    "write",
  );
}