/**
 * Giveaway reads for the UI.
 *
 * Privacy rule enforced here: the public-facing projections never include
 * account timestamps, join timestamps, per-user message counts, or full Discord
 * user objects. Participant rows are admin-only and only ever expose a Discord
 * ID plus entry bookkeeping, which is what the operator needs to manage entries.
 */

import { all, first, nowMs } from "./db";

export interface PublicGiveaway {
  id: string;
  guild_id: string;
  guild_name: string | null;
  guild_icon: string | null;
  channel_id: string;
  title: string;
  description: string;
  prize: string;
  prize_image_url: string | null;
  prize_count: number;
  winner_count: number;
  status: string;
  ended_reason: string | null;
  starts_at: number | null;
  ends_at: number | null;
  created_at: number;
  participant_count: number;
  entry_count: number;
  max_entries_per_user: number;
  entry_limit: number;
  required_role_count: number;
  required_mode: string;
  blacklist_role_count: number;
  channel_restricted: boolean;
  min_account_age_days: number;
  min_guild_join_days: number;
  requires_membership: boolean;
  min_messages: number;
  message_count_scope: string;
  message_count_channel_count: number;
  participant_role_id: string | null;
  seed_commitment: string | null;
  total_draws: number;
}

const PUBLIC_SELECT = `
  SELECT g.id, g.guild_id, gu.name AS guild_name, gu.icon_url AS guild_icon,
         g.channel_id, g.title, g.description, g.prize, g.prize_image_url,
         g.prize_count, g.winner_count, g.status, g.ended_reason,
         g.starts_at, g.ends_at, g.created_at,
         COALESCE(s.participant_count, 0) AS participant_count,
         COALESCE(s.entry_count, 0)      AS entry_count,
         g.max_entries_per_user, g.entry_limit,
         g.required_role_ids, g.required_mode, g.blacklist_role_ids, g.allowed_channel_ids,
         g.min_account_age_days, g.min_guild_join_days, g.entrants_require_membership,
         g.min_messages, g.message_count_scope, g.message_count_channel_ids,
         g.participant_role_id, g.seed_commitment, g.total_draws
    FROM giveaways g
    LEFT JOIN giveaway_stats s  ON s.giveaway_id = g.id
    LEFT JOIN guilds gu         ON gu.id = g.guild_id
`;

interface RawGiveawayRow {
  id: string;
  guild_id: string;
  guild_name: string | null;
  guild_icon: string | null;
  channel_id: string;
  title: string;
  description: string;
  prize: string;
  prize_image_url: string | null;
  prize_count: number;
  winner_count: number;
  status: string;
  ended_reason: string | null;
  starts_at: number | null;
  ends_at: number | null;
  created_at: number;
  participant_count: number;
  entry_count: number;
  max_entries_per_user: number;
  entry_limit: number;
  required_role_ids: string;
  required_mode: string;
  blacklist_role_ids: string;
  allowed_channel_ids: string;
  min_account_age_days: number;
  min_guild_join_days: number;
  entrants_require_membership: number;
  min_messages: number;
  message_count_scope: string;
  message_count_channel_ids: string;
  participant_role_id: string | null;
  seed_commitment: string | null;
  total_draws: number;
}

function parseJsonArray(raw: string | null): string[] {
  if (!raw) return [];
  try {
    const parsed: unknown = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed.map(String) : [];
  } catch {
    return [];
  }
}

/** Shape rows for the client: counts only, never the ID lists themselves. */
function toPublic(row: RawGiveawayRow): PublicGiveaway {
  const required = parseJsonArray(row.required_role_ids);
  const blacklist = parseJsonArray(row.blacklist_role_ids);
  const messageChannels = parseJsonArray(row.message_count_channel_ids);
  return {
    id: row.id,
    guild_id: row.guild_id,
    guild_name: row.guild_name,
    guild_icon: row.guild_icon,
    channel_id: row.channel_id,
    title: row.title,
    description: row.description,
    prize: row.prize,
    prize_image_url: row.prize_image_url,
    prize_count: row.prize_count,
    winner_count: row.winner_count,
    status: row.status,
    ended_reason: row.ended_reason,
    starts_at: row.starts_at,
    ends_at: row.ends_at,
    created_at: row.created_at,
    participant_count: row.participant_count,
    entry_count: row.entry_count,
    max_entries_per_user: row.max_entries_per_user,
    entry_limit: row.entry_limit,
    // Only the counts are public; the actual role/channel IDs are admin data.
    required_role_count: required.length,
    required_mode: row.required_mode,
    blacklist_role_count: blacklist.length,
    channel_restricted: parseJsonArray(row.allowed_channel_ids).length > 0,
    min_account_age_days: row.min_account_age_days,
    min_guild_join_days: row.min_guild_join_days,
    requires_membership: Boolean(row.entrants_require_membership),
    min_messages: row.min_messages,
    message_count_scope: row.message_count_scope,
    message_count_channel_count: messageChannels.length,
    participant_role_id: row.participant_role_id,
    seed_commitment: row.seed_commitment,
    total_draws: row.total_draws,
  };
}

export async function getPublicGiveaway(id: string): Promise<PublicGiveaway | null> {
  const row = await first<RawGiveawayRow>(`${PUBLIC_SELECT} WHERE g.id = ?`, [id]);
  return row ? toPublic(row) : null;
}

/** Public listing, newest first. Only non-scheduled giveaways are shown. */
export async function listPublicGiveaways(limit = 30, status?: string): Promise<PublicGiveaway[]> {
  const rows = await all<RawGiveawayRow>(
    `${PUBLIC_SELECT}
      WHERE g.status != 'scheduled'
        AND (? IS NULL OR g.status = ?)
      ORDER BY
        CASE g.status WHEN 'running' THEN 0 WHEN 'paused' THEN 1 ELSE 2 END,
        g.ends_at IS NULL, g.ends_at ASC
      LIMIT ?`,
    [status ?? null, status ?? null, Math.min(Math.max(limit, 1), 100)],
  );
  return rows.map(toPublic);
}

/** All giveaways for a guild, including scheduled (admin view). */
export async function listGuildGiveaways(
  guildId: string,
  limit = 50,
): Promise<PublicGiveaway[]> {
  const rows = await all<RawGiveawayRow>(
    `${PUBLIC_SELECT} WHERE g.guild_id = ? ORDER BY g.created_at DESC LIMIT ?`,
    [guildId, Math.min(Math.max(limit, 1), 200)],
  );
  return rows.map(toPublic);
}

/** The single open giveaway for a guild, if any (the one-active rule). */
export async function getActiveGiveaway(guildId: string): Promise<PublicGiveaway | null> {
  const row = await first<RawGiveawayRow>(
    `${PUBLIC_SELECT}
      WHERE g.guild_id = ? AND g.status IN ('scheduled','running','paused')
      ORDER BY g.created_at DESC LIMIT 1`,
    [guildId],
  );
  return row ? toPublic(row) : null;
}

export interface WinnerRow {
  round: number;
  rank: number;
  user_id: string;
  score: string;
  awarded_at: number;
}

export async function listWinners(giveawayId: string): Promise<{
  latest: WinnerRow[];
  history: WinnerRow[];
}> {
  const rows = await all<WinnerRow>(
    `SELECT round, rank, user_id, score, awarded_at
       FROM giveaway_winners WHERE giveaway_id = ?
      ORDER BY round DESC, rank ASC`,
    [giveawayId],
  );
  const latestRound = rows.length > 0 ? Number(rows[0]?.round ?? 0) : 0;
  return {
    latest: rows.filter((row) => Number(row.round) === latestRound),
    history: rows,
  };
}

export interface DrawRow {
  id: string;
  round: number;
  participant_count: number;
  winner_count: number;
  seed: string;
  seed_commitment: string;
  participant_digest: string;
  trigger_reason: string;
  created_at: number;
}

export async function listDraws(giveawayId: string): Promise<DrawRow[]> {
  return await all<DrawRow>(
    `SELECT id, round, participant_count, winner_count, server_seed AS seed,
            seed_commitment, participant_digest, trigger_reason, created_at
       FROM giveaway_draws WHERE giveaway_id = ?
      ORDER BY round DESC`,
    [giveawayId],
  );
}

export async function getDrawManifest(drawId: string): Promise<{
  id: string;
  giveaway_id: string;
  round: number;
  method: string;
  algorithm_version: string;
  manifest: unknown;
} | null> {
  return await first(
    `SELECT id, giveaway_id, round, method, algorithm_version, manifest_json
       FROM giveaway_draws WHERE id = ?`,
    [drawId],
  );
}

/** Admin-only participant list. Exposes IDs and bookkeeping, nothing else. */
export interface ParticipantRow {
  user_id: string;
  entries: number;
  first_joined_at: number | null;
  last_joined_at: number | null;
  status: string;
  invalid_reason: string | null;
  message_count: number | null;
}

export async function listParticipants(
  giveawayId: string,
  options: { limit?: number; offset?: number; search?: string; status?: string } = {},
): Promise<{ rows: ParticipantRow[]; total: number }> {
  const limit = Math.min(Math.max(options.limit ?? 25, 1), 100);
  const offset = Math.max(options.offset ?? 0, 0);
  const search = options.search?.trim() ?? "";
  const like = `%${search}%`;
  const status = options.status && options.status !== "all" ? options.status : null;

  const rows = await all<ParticipantRow>(
    `SELECT e.user_id,
            COUNT(*)                              AS entries,
            MIN(e.joined_at)                      AS first_joined_at,
            MAX(e.joined_at)                      AS last_joined_at,
            MAX(e.invalid_reason)                 AS invalid_reason,
            CASE
              WHEN SUM(CASE WHEN e.status = 'winner' THEN 1 ELSE 0 END) > 0 THEN 'winner'
              WHEN SUM(CASE WHEN e.status = 'disqualified' THEN 1 ELSE 0 END) = COUNT(*) THEN 'disqualified'
              WHEN SUM(CASE WHEN e.status = 'invalid' THEN 1 ELSE 0 END) = COUNT(*) THEN 'invalid'
              ELSE 'valid'
            END                                  AS status,
            c.message_count                      AS message_count
       FROM giveaway_entries e
       LEFT JOIN message_counters c
              ON c.guild_id = (SELECT guild_id FROM giveaways WHERE id = e.giveaway_id)
             AND c.user_id = e.user_id
      WHERE e.giveaway_id = ?
        AND (? = '' OR e.user_id LIKE ?)
        AND (? IS NULL OR e.status = ?)
      GROUP BY e.user_id
      ORDER BY MAX(e.joined_at) DESC
      LIMIT ? OFFSET ?`,
    [giveawayId, search, like, status, status, limit, offset],
  );

  const totalRow = await first<{ count: number }>(
    `SELECT COUNT(DISTINCT user_id) AS count
       FROM giveaway_entries
      WHERE giveaway_id = ?
        AND (? = '' OR user_id LIKE ?)
        AND (? IS NULL OR status = ?)`,
    [giveawayId, search, like, status, status],
  );

  return { rows, total: Number(totalRow?.count ?? 0) };
}

export interface AuditRow {
  id: number;
  guild_id: string;
  giveaway_id: string | null;
  action: string;
  actor_id: string | null;
  actor_name: string | null;
  source: string;
  target_id: string | null;
  outcome: string;
  created_at: number;
}

export async function listAudit(
  giveawayId: string,
  limit = 25,
): Promise<AuditRow[]> {
  return await all<AuditRow>(
    `SELECT id, guild_id, giveaway_id, action, actor_id, actor_name,
            source, target_id, outcome, created_at
       FROM audit_log WHERE giveaway_id = ?
      ORDER BY id DESC LIMIT ?`,
    [giveawayId, Math.min(Math.max(limit, 1), 100)],
  );
}

export interface EventRow {
  id: number;
  type: string;
  payload_json: string;
  created_at: number;
}

/** Recent events, used to drive the live updates without a websocket. */
export async function listEventsSince(
  giveawayId: string,
  sinceId = 0,
  limit = 25,
): Promise<Array<{ id: number; type: string; payload: unknown; created_at: number }>> {
  const rows = await all<EventRow>(
    `SELECT id, type, payload_json, created_at
       FROM giveaway_events
      WHERE giveaway_id = ? AND id > ?
      ORDER BY id ASC LIMIT ?`,
    [giveawayId, sinceId, Math.min(Math.max(limit, 1), 100)],
  );
  return rows.map((row) => {
    let payload: unknown = {};
    try {
      payload = JSON.parse(row.payload_json);
    } catch {
      payload = {};
    }
    return { id: Number(row.id), type: row.type, payload, created_at: Number(row.created_at) };
  });
}

export async function latestEventId(giveawayId: string): Promise<number> {
  const row = await first<{ id: number }>(
    "SELECT COALESCE(MAX(id), 0) AS id FROM giveaway_events WHERE giveaway_id = ?",
    [giveawayId],
  );
  return Number(row?.id ?? 0);
}

export interface Analytics {
  giveaways: { total: number; running: number; scheduled: number; paused: number; ended: number };
  participation: { entries: number; participants: number; winners: number };
  activity: {
    tracked_users: number;
    exact_users: number;
    backfilled_users: number;
    estimated_users: number;
  };
  recent: Array<{
    id: string;
    title: string;
    status: string;
    entry_count: number;
    participant_count: number;
    winner_count: number;
  }>;
}

export async function getAnalytics(guildId: string): Promise<Analytics> {
  const giveawayTotals = await first<Record<string, number>>(
    `SELECT COUNT(*) AS total,
            SUM(CASE WHEN status = 'running' THEN 1 ELSE 0 END)   AS running,
            SUM(CASE WHEN status = 'scheduled' THEN 1 ELSE 0 END) AS scheduled,
            SUM(CASE WHEN status = 'paused' THEN 1 ELSE 0 END)    AS paused,
            SUM(CASE WHEN status = 'ended' THEN 1 ELSE 0 END)    AS ended
       FROM giveaways WHERE guild_id = ?`,
    [guildId],
  );

  const participation = await first<Record<string, number>>(
    `SELECT COALESCE(SUM(s.entry_count), 0)       AS entries,
            COALESCE(SUM(s.participant_count), 0) AS participants,
            COALESCE(SUM(s.winner_count), 0)      AS winners
       FROM giveaway_stats s
       JOIN giveaways g ON g.id = s.giveaway_id
      WHERE g.guild_id = ?`,
    [guildId],
  );

  const activity = await first<Record<string, number>>(
    `SELECT COUNT(*) AS tracked_users,
            SUM(CASE WHEN exactness = 'exact' THEN 1 ELSE 0 END)      AS exact_users,
            SUM(CASE WHEN exactness = 'backfilled' THEN 1 ELSE 0 END) AS backfilled_users,
            SUM(CASE WHEN exactness = 'estimated' THEN 1 ELSE 0 END)  AS estimated_users
       FROM message_counters WHERE guild_id = ?`,
    [guildId],
  );

  const recent = await all<{
    id: string;
    title: string;
    status: string;
    entry_count: number;
    participant_count: number;
    winner_count: number;
  }>(
    `SELECT g.id, g.title, g.status,
            COALESCE(s.entry_count, 0)      AS entry_count,
            COALESCE(s.participant_count, 0) AS participant_count,
            COALESCE(s.winner_count, 0)      AS winner_count
       FROM giveaways g
       LEFT JOIN giveaway_stats s ON s.giveaway_id = g.id
      WHERE g.guild_id = ?
      ORDER BY g.created_at DESC
      LIMIT 8`,
    [guildId],
  );

  const n = (value: number | null | undefined): number => Number(value ?? 0);
  return {
    giveaways: {
      total: n(giveawayTotals?.total),
      running: n(giveawayTotals?.running),
      scheduled: n(giveawayTotals?.scheduled),
      paused: n(giveawayTotals?.paused),
      ended: n(giveawayTotals?.ended),
    },
    participation: {
      entries: n(participation?.entries),
      participants: n(participation?.participants),
      winners: n(participation?.winners),
    },
    activity: {
      tracked_users: n(activity?.tracked_users),
      exact_users: n(activity?.exact_users),
      backfilled_users: n(activity?.backfilled_users),
      estimated_users: n(activity?.estimated_users),
    },
    recent,
  };
}

/** Server-side rendered relative time helper shared by components. */
export function relativeTime(timestamp: number | null | undefined): string {
  if (!timestamp) return "—";
  const delta = timestamp - nowMs();
  const abs = Math.abs(delta);
  const units: Array<[Intl.RelativeTimeFormatUnit, number]> = [
    ["day", 86_400_000],
    ["hour", 3_600_000],
    ["minute", 60_000],
    ["second", 1000],
  ];
  const formatter = new Intl.RelativeTimeFormat("en", { numeric: "auto" });
  for (const [unit, ms] of units) {
    if (abs >= ms || unit === "second") {
      return formatter.format(Math.round(delta / ms), unit);
    }
  }
  return "—";
}
