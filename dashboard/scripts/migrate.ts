/**
 * Migration runner.
 *
 * Reads the *same* SQL files the Python bot uses (`shared/migrations`), so both
 * runtimes can never drift. Files are applied in filename order inside one
 * atomic batch, and a checksum is stored per file so an already-applied
 * migration that later changes is rejected rather than silently ignored.
 *
 * Usage:
 *   node scripts/migrate.mjs            # apply pending
 *   node scripts/migrate.mjs --status   # list applied/pending
 *   node scripts/migrate.mjs --reset    # drop everything, then apply
 */

import { createHash } from "node:crypto";
import { existsSync, readFileSync, readdirSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

/**
 * @typedef {{ rows: Array<Record<string, unknown>> }} DbResult
 * @typedef {{ sql: string, args?: unknown[] }} Stmt
 */
import { all, executeScript } from "./db.mjs";

const here = dirname(fileURLToPath(import.meta.url));

function migrationsDir(): string {
  if (process.env.MIGRATIONS_DIR) return resolve(process.env.MIGRATIONS_DIR);
  // Default: the monorepo's shared/migrations, then a local copy.
  const candidates = [
    resolve(here, "../../shared/migrations"),
    resolve(here, "../shared/migrations"),
    resolve(process.cwd(), "shared/migrations"),
    resolve(process.cwd(), "../shared/migrations"),
  ];
  for (const candidate of candidates) {
    if (existsSync(candidate)) return candidate;
  }
  throw new Error(
    `Could not find migrations. Looked in:\n  ${candidates.join("\n  ")}\n` +
      "Set MIGRATIONS_DIR to override.",
  );
}

/**
 * Split a migration into statements.
 *
 * Must mirror `bot/giveaway_bot/db.py::split_statements` exactly: a line whose
 * first token is `; statement-breakpoint` ends the current statement.
 */
export function splitStatements(sql: string): string[] {
  const chunks: string[] = [];
  let current: string[] = [];
  for (const line of sql.split("\n")) {
    if (line.trim().toLowerCase().startsWith("; statement-breakpoint")) {
      const statement = current.join("\n").trim();
      if (statement) chunks.push(statement);
      current = [];
      continue;
    }
    current.push(line);
  }
  const tail = current.join("\n").trim();
  if (tail) chunks.push(tail);
  return chunks.filter((chunk) => {
    const body = chunk
      .split("\n")
      .map((l) => l.trim())
      .filter((l) => l && !l.startsWith("--"));
    return body.length > 0;
  });
}

/** Count top-level statements, ignoring comments and quoted text. */
function countStatements(sql: string): number {
  const text = sql.replace(/--[^\n]*/g, "");
  let count = 0;
  let inString = false;
  for (let i = 0; i < text.length; i += 1) {
    const char = text[i];
    if (char === "'") {
      const previous = text[i - 1];
      if (!inString || previous !== "\\") inString = !inString;
    } else if (char === ";" && !inString) {
      count += 1;
    }
  }
  return count;
}

function loadMigrations(dir: string): Array<{ name: string; sql: string; checksum: string }> {
  const files = readdirSync(dir)
    .filter((f) => f.endsWith(".sql"))
    .sort((a, b) => a.localeCompare(b, "en"));

  if (files.length === 0) throw new Error(`No .sql migrations found in ${dir}`);

  return files.map((name) => {
    // A BOM is not whitespace. Node's utf8 decoding keeps one, so it would end
    // up inside the first statement's text and inside the checksum. SQLite
    // ignores it, but Turso's parser rejects the statement outright - which
    // failed a hosted deploy on a migration every local test had passed.
    // Stripped everywhere, not only at the start. Mirrors the bot's loader.
    const sql = readFileSync(join(dir, name), "utf8").replace(/\uFEFF/g, "");
    return {
      name,
      sql,
      checksum: createHash("sha256").update(sql).digest("hex").slice(0, 32),
    };
  });
}

async function appliedMap(): Promise<Map<string, string>> {
  const rows = (await all(
    "SELECT filename, checksum FROM schema_migrations")) as Array<{ filename: string; checksum: string }>;
  return new Map(rows.map((row) => [row.filename, row.checksum]));
}

async function ensureBookkeeping(): Promise<void> {
  await executeScript([
    `CREATE TABLE IF NOT EXISTS schema_migrations (
       filename    TEXT PRIMARY KEY,
       checksum    TEXT NOT NULL,
       applied_at  INTEGER NOT NULL,
       duration_ms INTEGER NOT NULL DEFAULT 0
     )`,
  ]);
}

async function main(): Promise<void> {
  const args = process.argv.slice(2);
  const statusOnly = args.includes("--status");
  const reset = args.includes("--reset");

  const dir = migrationsDir();
  const migrations = loadMigrations(dir);
  await ensureBookkeeping();

  if (reset) {
    console.log("Resetting the database (dropping all known tables)...");
    const tables = [
      "giveaway_role_tasks",
      "message_counter_channels",
      "message_channel_state",
      "message_counters",
      "command_queue",
      "giveaway_events",
      "rate_limits",
      "oauth_states",
      "audit_log",
      "giveaway_winners",
      "giveaway_draws",
      "giveaway_stats",
      "giveaway_entries",
      "giveaways",
      "guild_admins",
      "guilds",
      "bot_state",
      // Dropped LAST, and this was the bug: schema_migrations survived, so
      // appliedMap() still reported every migration as applied, `pending` came
      // back empty, and the runner printed "Database is already up to date" with
      // zero application tables. A --reset that leaves an empty schema is worse
      // than one that fails.
      "schema_migrations",
    ];
    // order matters for FKs; PRAGMA off so ordering issues cannot abort the drop
    await executeScript(["PRAGMA foreign_keys = OFF"]);
    await executeScript(tables.map((t) => `DROP TABLE IF EXISTS ${t}`));
    await executeScript(["PRAGMA foreign_keys = ON"]);
    await ensureBookkeeping();
    // Nothing may be left claiming to be applied.
    const afterReset = await appliedMap();
    if (afterReset.size > 0) {
      throw new Error(
        `--reset did not clear schema_migrations (${afterReset.size} row(s) remain); ` +
          "the schema is empty but the ledger still claims migrations ran",
      );
    }
  }

  const known = await appliedMap();
  const pending = migrations.filter((m) => !known.has(m.name));

  if (statusOnly) {
    console.log(`Migrations directory: ${dir}`);
    for (const migration of migrations) {
      const appliedChecksum = known.get(migration.name);
      const state =
        appliedChecksum === undefined
          ? "pending"
          : appliedChecksum === migration.checksum
            ? "applied"
            : "MODIFIED (checksum mismatch)";
      console.log(`  [${state}] ${migration.name}`);
    }
    console.log(`\n${migrations.length} migration(s), ${pending.length} pending`);
    return;
  }

  // Checked before the pending check below, and deliberately not inside it: this
  // guard used to sit after an early `return` for the nothing-pending case,
  // so it never ran in the one situation it exists for - an already-applied
  // migration edited on disk. A rewritten migration was then skipped silently,
  // and the two runtimes could drift onto different schemas while both reported
  // success.
  for (const migration of migrations) {
    const appliedChecksum = known.get(migration.name);
    if (appliedChecksum !== undefined && appliedChecksum !== migration.checksum) {
      throw new Error(
        `Migration ${migration.name} changed after being applied ` +
          `(expected ${appliedChecksum}, got ${migration.checksum}). ` +
          "Migrations are immutable - add a new file instead.",
      );
    }
  }

  if (pending.length === 0) {
    console.log("Database is already up to date.");
    return;
  }

  console.log(`Applying ${pending.length} migration(s) from ${dir}`);
  for (const migration of pending) {
    const statements = splitStatements(migration.sql);
    const problems: string[] = [];
    statements.forEach((statement, index) => {
      const count = countStatements(statement);
      if (count > 1) {
        problems.push(
          `statement ${index} contains ${count} statements; a '; statement-breakpoint' line is missing`,
        );
      }
    });
    if (problems.length > 0) {
      throw new Error(`Migration ${migration.name} is malformed: ${problems.join("; ")}`);
    }

    const started = Date.now();
    // Atomic: the migration body and its bookkeeping row land together.
    await executeScript([
      ...statements,
      `INSERT INTO schema_migrations (filename, checksum, applied_at, duration_ms)
       VALUES ('${migration.name}', '${migration.checksum}', ${Date.now()}, 0)`,
    ]);
    console.log(`  + ${migration.name} (${statements.length} statements, ${Date.now() - started}ms)`);
  }
  console.log("Done.");
}

main().catch((error) => {
  console.error(`\nMigration failed: ${error instanceof Error ? error.message : String(error)}`);
  process.exit(1);
});


