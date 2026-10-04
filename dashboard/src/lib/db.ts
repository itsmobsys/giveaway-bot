/**
 * Database client for Turso / libSQL.
 *
 * Serverless-safe by design:
 *  - a single client per process, reused across warm invocations
 *  - no long-lived connections, no transaction held across an await
 *  - every query is bounded (LIMIT + index) so no request can fan out
 *
 * Falls back to a local SQLite file when TURSO_DATABASE_URL is unset, so the
 * dashboard can be developed without provisioning a database.
 */

import { createClient, type Client, type InValue } from "@libsql/client";
import { existsSync, mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";

type SqlValue = InValue;

interface GlobalWithClient {
  __giveawayDb?: Client;
}

const globalRef = globalThis as GlobalWithClient;

function localPath(): string {
  return process.env.SQLITE_PATH ?? "./data/giveaways.db";
}

/** Lazily create the client so importing this module never throws at build time. */
export function db(): Client {
  if (globalRef.__giveawayDb) return globalRef.__giveawayDb;

  const url = process.env.TURSO_DATABASE_URL;
  const authToken = process.env.TURSO_AUTH_TOKEN;

  let client: Client;
  if (url && url.startsWith("libsql://")) {
    client = createClient({ url, authToken });
  } else {
    // Local development / self-host without Turso.
    const path = resolve(localPath());
    if (!existsSync(path)) mkdirSync(dirname(path), { recursive: true });
    client = createClient({ url: `file:${path}` });
  }

  globalRef.__giveawayDb = client;
  return client;
}

export const isTurso = (): boolean =>
  Boolean(process.env.TURSO_DATABASE_URL?.startsWith("libsql://"));

/** Rows as plain objects. */
export async function all<T = Record<string, SqlValue>>(
  sql: string,
  params: SqlValue[] = [],
): Promise<T[]> {
  const result = await db().execute({ sql, args: params });
  return result.rows as unknown as T[];
}

export async function first<T = Record<string, SqlValue>>(
  sql: string,
  params: SqlValue[] = [],
): Promise<T | null> {
  const rows = await all<T>(sql, params);
  return rows.length > 0 ? (rows[0] as T) : null;
}

export async function run(
  sql: string,
  params: SqlValue[] = [],
): Promise<{ rowsAffected: number; lastInsertRowid: bigint | number | undefined }> {
  const result = await db().execute({ sql, args: params });
  return {
    rowsAffected: result.rowsAffected,
    lastInsertRowid: result.lastInsertRowid,
  };
}

export async function executeScript(statements: string[]): Promise<void> {
  if (statements.length === 0) return;
  await db().batch(statements.map((sql) => ({ sql, args: [] })), "write");
}

/**
 * Run a set of writes atomically.
 *
 * Turso/libSQL has interactive transactions; `batch` is atomic by default in
 * write mode. We use `batch` rather than BEGIN/COMMIT so nothing can leak a
 * transaction across an await in a serverless function.
 */
export async function transact(
  operations: Array<{ sql: string; args?: SqlValue[] }>,
): Promise<void> {
  if (operations.length === 0) return;
  await db().batch(
    operations.map((operation) => ({ sql: operation.sql, args: operation.args ?? [] })),
    "write",
  );
}

/** Current epoch milliseconds. */
export function nowMs(): number {
  return Date.now();
}
