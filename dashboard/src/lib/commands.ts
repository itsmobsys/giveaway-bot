/**
 * Dashboard -> bot commands.
 *
 * The dashboard never calls Discord. It appends a row to `command_queue`; the
 * Python bot claims it atomically, re-validates everything (including that a
 * channel or role still exists), executes it, and records the result.
 *
 * Consequences that matter:
 *  - a stolen dashboard session cannot invent bot capabilities; the bot only
 *    accepts the whitelisted `kind` values it knows
 *  - every action is auditable because the bot writes the audit row
 *  - failures are visible to the operator instead of being swallowed
 */

import { z } from "zod";

import { all, first, nowMs, run } from "./db";

/** Must stay in sync with `bot/giveaway_bot/models.py::CommandKind`. */
export const COMMAND_KINDS = [
  "giveaway.create",
  "giveaway.update",
  "giveaway.pause",
  "giveaway.resume",
  "giveaway.extend",
  "giveaway.shorten",
  "giveaway.end",
  "giveaway.cancel",
  "giveaway.reroll",
  "giveaway.reveal",
  "entry.disqualify",
  "entry.restore",
  "giveaway.message_requirement",
  "giveaway.activity_revalidate",
  "giveaway.announce",
] as const;

export type CommandKind = (typeof COMMAND_KINDS)[number];

const snowflake = z
  .string()
  .regex(/^[0-9]{15,25}$/, "Must be a valid Discord ID");

const snowflakeList = z
  .array(snowflake)
  .max(200)
  .transform((items) => [...new Set(items)]);

/** Duration: either minutes as a number, or a `30m` / `2h30m` / `3d` string. */
const duration = z
  .union([z.number().int().min(30_000).max(365 * 24 * 3_600_000), z.string().min(1).max(40)])
  .transform((value, ctx) => {
    if (typeof value === "number") return value;
    const matches = value.toLowerCase().matchAll(/(\d+)\s*([smhdw])/g);
    const units: Record<string, number> = {
      s: 1000,
      m: 60_000,
      h: 3_600_000,
      d: 86_400_000,
      w: 604_800_000,
    };
    let total = 0;
    for (const match of matches) {
      total += Number(match[1]) * (units[match[2] ?? "m"] ?? 60_000);
    }
    if (total < 30_000 || total > 365 * 24 * 3_600_000) {
      ctx.addIssue({ code: z.ZodIssueCode.custom, message: "Use 30m, 2h30m or 3d." });
      return z.NEVER;
    }
    return total;
  });

export const createSchema = z.object({
  title: z.string().trim().min(1).max(256),
  prize: z.string().trim().max(512).default(""),
  description: z.string().trim().max(4000).default(""),
  // No channel_id: giveaways are always posted in the bot's own channel, which
  // is deployment configuration. Admins cannot choose (or redirect) it.
  duration,
  winner_count: z.number().int().min(1).max(20).default(1),
  prize_count: z.number().int().min(1).max(20).default(1),
  prize_image_url: z
    .string()
    .url()
    .refine((url) => /^https:\/\/(cdn\.discordapp\.com|media\.discordapp\.net)\//i.test(url), {
      message: "Must be a Discord CDN image URL.",
    })
    .optional()
    .or(z.literal("")),
  max_entries_per_user: z.number().int().min(1).max(100).default(1),
  entry_limit: z.number().int().min(0).max(1_000_000).default(0),
  required_role_ids: snowflakeList.default([]),
  required_mode: z.enum(["any", "all"]).default("any"),
  blacklist_role_ids: snowflakeList.default([]),
  allowed_channel_ids: snowflakeList.default([]),
  min_account_age_days: z.number().int().min(0).max(3650).default(0),
  min_guild_join_days: z.number().int().min(0).max(3650).default(0),
  entrants_require_membership: z.boolean().default(true),
  min_messages: z.number().int().min(0).max(100_000).default(0),
  message_count_channel_ids: snowflakeList.default([]),
  message_count_scope: z.enum(["guild", "channel"]).default("guild"),
  message_count_ignore_bots: z.boolean().default(true),
});

export const updateSchema = createSchema.partial();

export const extendSchema = z.object({ duration });
export const shortenSchema = z.object({ duration });
export const endSchema = z.object({
  reason: z.string().trim().max(200).optional(),
  revalidate_activity: z.boolean().default(true),
});
export const cancelSchema = z.object({ reason: z.string().trim().max(200).optional() });
export const rerollSchema = z.object({ reason: z.string().trim().max(200).optional() });
export const entrySchema = z.object({
  user_id: snowflake,
  reason: z.string().trim().max(200).optional(),
});
export const messageRequirementSchema = z.object({
  min_messages: z.number().int().min(0).max(100_000),
  message_count_scope: z.enum(["guild", "channel"]).default("guild"),
  message_count_channel_ids: snowflakeList.default([]),
  message_count_ignore_bots: z.boolean().default(true),
});
export const revalidateSchema = z.object({});

export interface EnqueueResult {
  commandId: number;
}

/** Append a command for the bot to execute. */
export async function enqueueCommand(input: {
  guildId: string;
  giveawayId?: string | null;
  kind: CommandKind;
  payload: Record<string, unknown>;
  requestedBy: string;
  requestedByName?: string | null;
  source?: string;
  priority?: number;
}): Promise<EnqueueResult> {
  const { lastInsertRowid } = await run(
    `INSERT INTO command_queue (
       guild_id, giveaway_id, kind, payload_json, requested_by,
       requested_by_name, source, status, priority, created_at
     ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)`,
    [
      input.guildId,
      input.giveawayId ?? null,
      input.kind,
      JSON.stringify(input.payload ?? {}),
      input.requestedBy,
      input.requestedByName ?? null,
      input.source ?? "dashboard",
      input.priority ?? 100,
      nowMs(),
    ],
  );

  return { commandId: Number(lastInsertRowid ?? 0) };
}

export interface CommandRow {
  id: number;
  kind: string;
  status: string;
  last_error: string | null;
  result_json: string | null;
  created_at: number;
  processed_at: number | null;
  attempts: number;
}

/** Read the state of one command, for the UI to show progress. */
export async function getCommand(id: number): Promise<CommandRow | null> {
  return await first<CommandRow>(
    `SELECT id, kind, status, last_error, result_json, created_at, processed_at, attempts
       FROM command_queue WHERE id = ?`,
    [id],
  );
}

/** Recent commands for a giveaway, newest first. */
export async function listCommands(
  giveawayId: string,
  limit = 10,
): Promise<CommandRow[]> {
  return await all<CommandRow>(
    `SELECT id, kind, status, last_error, result_json, created_at, processed_at, attempts
       FROM command_queue
      WHERE giveaway_id = ?
      ORDER BY id DESC
      LIMIT ?`,
    [giveawayId, Math.min(Math.max(limit, 1), 50)],
  );
}

/**
 * Whether the last command for a giveaway is still running.
 *
 * The UI disables its action buttons while this is true, which prevents an
 * admin from queueing five ends in a row and getting confused by the results.
 */
export async function hasPendingCommand(giveawayId: string): Promise<boolean> {
  const row = await first<{ count: number }>(
    `SELECT COUNT(*) AS count FROM command_queue
      WHERE giveaway_id = ? AND status IN ('pending', 'claimed')`,
    [giveawayId],
  );
  return Number(row?.count ?? 0) > 0;
}
