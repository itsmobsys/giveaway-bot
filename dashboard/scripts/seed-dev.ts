/**
 * End-to-end smoke test against a real database.
 *
 * Exercises the exact query shapes used by the UI so a bad column name or a
 * broken join fails here instead of in production. The fairness cross-language
 * check lives in scripts/verify-fairness.ts.
 *
 *   node scripts/seed-dev.mjs
 */

import { all, executeScript, first } from "./db.mjs";
// Reuse the real derivation so the seeded draw actually verifies, rather than
// hard-coding scores that would fail the dashboard's own verification panel.
import { drawForSeed, METHOD } from "../src/lib/fairness.ts";

const now = Date.now();
const HOUR = 3_600_000;
const guildId = "123456789012345678";
const channelId = "234567890123456789";
const giveawayId = "gw_demo_0001";

function log(step: string, detail = ""): void {
  console.log(`  ${detail ? `[ok] ` : ""}${step}${detail ? ` ${detail}` : ""}`);
}

async function main(): Promise<void> {
  console.log("Seeding development data");
  console.log("=".repeat(60));

  await executeScript([
    `DELETE FROM giveaway_events WHERE giveaway_id = '${giveawayId}'`,
    `DELETE FROM giveaway_winners WHERE giveaway_id = '${giveawayId}'`,
    `DELETE FROM giveaway_draws  WHERE giveaway_id = '${giveawayId}'`,
    `DELETE FROM giveaway_stats  WHERE giveaway_id = '${giveawayId}'`,
    `DELETE FROM giveaway_entries WHERE giveaway_id = '${giveawayId}'`,
    `DELETE FROM audit_log WHERE giveaway_id = '${giveawayId}'`,
    `DELETE FROM giveaways WHERE id = '${giveawayId}'`,
    `DELETE FROM guild_admins WHERE guild_id = '${guildId}'`,
    `DELETE FROM guilds WHERE id = '${guildId}'`,
    `DELETE FROM message_counters WHERE guild_id = '${guildId}'`,
    `DELETE FROM message_channel_state WHERE guild_id = '${guildId}'`,
  ]);

  // giveaway_stats has an FK to giveaways, so the parent row must exist first.
  await executeScript([
    `INSERT INTO guilds (id, name, icon_url, owner_id, member_count, bot_present, synced_at, created_at, updated_at)
     VALUES ('${guildId}', 'Demo Community', NULL, '111111111111111111', 1284, 1, ${now}, ${now}, ${now})`,
    `INSERT INTO giveaways (
        id, guild_id, channel_id, message_id, status, title, description, prize,
        prize_count, winner_count, entry_limit, max_entries_per_user,
        starts_at, ends_at, original_ends_at,
        required_role_ids, required_mode, blacklist_role_ids, allowed_channel_ids,
        min_account_age_days, min_guild_join_days, entrants_require_membership,
        seed_commitment, min_messages, message_count_channel_ids, message_count_scope,
        created_by, version, created_at, updated_at
     ) VALUES (
        '${giveawayId}', '${guildId}', '${channelId}', NULL, 'running',
        'Steam key giveaway',
        'Three keys, one winner each. Good luck!',
        '3x Steam key',
        3, 2, 0, 1,
        ${now}, ${now + 6 * HOUR}, ${now + 6 * HOUR},
        '[]', 'any', '[]', '[]',
        0, 0, 1,
        '${"a".repeat(64)}', 25, '[]', 'guild',
        '111111111111111111', 1, ${now}, ${now}
     )`,
    `INSERT INTO giveaway_stats (giveaway_id, participant_count, entry_count, winner_count, updated_at)
     VALUES ('${giveawayId}', 0, 0, 0, ${now})`,
  ]);
  log("giveaway created", giveawayId);

  // Participants, including a multi-entry user.
  const entrants = [
    "100000000000000001",
    "100000000000000002",
    "100000000000000003",
    "100000000000000004",
    "100000000000000005",
    "100000000000000006",
    "100000000000000007",
  ];
  for (const [index, userId] of entrants.entries()) {
    await executeScript([
      `INSERT INTO giveaway_entries (
          giveaway_id, user_id, entry_seq, account_created_at, guild_joined_at,
          status, snapshot_json, role_granted_at, grant_source, joined_at, updated_at
       ) VALUES (
          '${giveawayId}', '${userId}', 1, ${now - 400 * 86_400_000}, ${now - 300 * 86_400_000},
          'valid', '{}', NULL, 'none', ${now - index * 60_000}, ${now}
       )`,
    ]);
  }
  // A second entry for the first user, and one disqualified member.
  await executeScript([
    `INSERT INTO giveaway_entries (
        giveaway_id, user_id, entry_seq, account_created_at, guild_joined_at,
        status, snapshot_json, role_granted_at, grant_source, joined_at, updated_at
     ) VALUES (
        '${giveawayId}', '${entrants[0]}', 2, ${now - 400 * 86_400_000}, ${now - 300 * 86_400_000},
        'valid', '{}', NULL, 'none', ${now - 30_000}, ${now}
     )`,
    `INSERT INTO giveaway_entries (
        giveaway_id, user_id, entry_seq, status, invalid_reason, joined_at, updated_at
     ) VALUES (
        '${giveawayId}', '100000000000000008', 1, 'disqualified', 'alt account', ${now}, ${now}
     )`,
  ]);
  log("participants added", `${entrants.length + 1} users`);

  // Message counters, so the admin table and analytics have data.
  for (const [index, userId] of entrants.entries()) {
    const count = 30 - index * 3;
    await executeScript([
      `INSERT INTO message_counters (
          guild_id, user_id, message_count, distinct_channels,
          first_message_at, last_message_at, window_started_at, exactness, updated_at
       ) VALUES (
          '${guildId}', '${userId}', ${count}, 2,
          ${now - 200 * 86_400_000}, ${now}, ${now - 200 * 86_400_000}, 'exact', ${now}
       )`,
    ]);
  }
  log("message counters added");

  await executeScript([
    `UPDATE giveaway_stats SET participant_count = 7, entry_count = 8, updated_at = ${now}
     WHERE giveaway_id = '${giveawayId}'`,
  ]);

  // A second, already-ended giveaway with a real draw so the public page,
  // history and verification have something to show.
  const pastId = "gw_demo_0002";
  await executeScript([
    `DELETE FROM giveaway_events WHERE giveaway_id = '${pastId}'`,
    `DELETE FROM giveaway_winners WHERE giveaway_id = '${pastId}'`,
    `DELETE FROM giveaway_draws WHERE giveaway_id = '${pastId}'`,
    `DELETE FROM giveaway_stats WHERE giveaway_id = '${pastId}'`,
    `DELETE FROM giveaway_entries WHERE giveaway_id = '${pastId}'`,
    `DELETE FROM giveaways WHERE id = '${pastId}'`,
    `INSERT INTO giveaways (
        id, guild_id, channel_id, status, ended_reason, title, description, prize,
        prize_count, winner_count, max_entries_per_user, starts_at, ends_at, original_ends_at,
        required_role_ids, required_mode, blacklist_role_ids, allowed_channel_ids,
        entrants_require_membership, seed_commitment, draw_round, total_draws,
        created_by, version, created_at, updated_at
     ) VALUES (
        '${pastId}', '${guildId}', '${channelId}', 'ended', 'timer', 'Ended giveaway',
        'This one has already been drawn.', 'Nitro month', 1, 1, 1,
        ${now - 48 * HOUR}, ${now - 24 * HOUR}, ${now - 24 * HOUR},
        '[]', 'any', '[]', '[]', 1, NULL, 1, 1,
        '111111111111111111', 1, ${now - 48 * HOUR}, ${now - 24 * HOUR}
     )`,
    `INSERT INTO giveaway_stats (giveaway_id, participant_count, entry_count, winner_count, updated_at)
     VALUES ('${pastId}', 0, 0, 0, ${now})`,
  ]);
  log("second (ended) giveaway created", pastId);

  // Give the ended giveaway a genuine draw, derived with the real algorithm, so
  // the public page, history and verification panel all have something real.
  const pastEntries = [
    { user_id: "200000000000000001", entry_seq: 1 },
    { user_id: "200000000000000002", entry_seq: 1 },
    { user_id: "200000000000000003", entry_seq: 1 },
    { user_id: "200000000000000004", entry_seq: 1 },
    { user_id: "200000000000000005", entry_seq: 1 },
  ];
  const pastSeed = "b".repeat(64);
  const { manifest, winners: pastWinners } = drawForSeed({
    giveawayId: pastId,
    seed: pastSeed,
    entries: pastEntries,
    winnerCount: 1,
    round: 1,
  });

  for (const [index, entry] of pastEntries.entries()) {
    await executeScript([
      `INSERT INTO giveaway_entries (
          giveaway_id, user_id, entry_seq, status, snapshot_json, joined_at, updated_at
       ) VALUES (
          '${pastId}', '${entry.user_id}', 1,
          '${pastWinners[0]?.user_id === entry.user_id ? "winner" : "valid"}',
          '{}', ${now - (30 - index) * 600_000}, ${now - 24 * HOUR}
       )`,
    ]);
  }

  const drawId = `draw_${pastId}_1_${now - 24 * HOUR}`;
  await executeScript([
    `INSERT INTO giveaway_draws (
        id, giveaway_id, round, method, algorithm_version, server_seed, seed_commitment,
        participant_digest, participant_count, eligible_count, winner_count,
        manifest_json, triggered_by, trigger_reason, duration_ms, created_at
     ) VALUES (
        '${drawId}', '${pastId}', 1, '${METHOD}', 'v1', '${pastSeed}',
        '${manifest.seed_commitment}', '${manifest.participant_digest}',
        ${manifest.participant_count}, ${manifest.participant_count}, ${manifest.winner_count},
        '${JSON.stringify(manifest).replace(/'/g, "''")}',
        NULL, 'timer', 42, ${now - 24 * HOUR}
     )`,
  ]);

  for (const winner of pastWinners) {
    await executeScript([
      `INSERT INTO giveaway_winners (
          giveaway_id, draw_id, round, user_id, rank, entry_seq, score,
          server_seed, seed_commitment, awarded_at
       ) VALUES (
          '${pastId}', '${drawId}', 1, '${winner.user_id}', ${winner.rank},
          ${winner.entry_seq}, '${winner.score}', '${pastSeed}',
          '${manifest.seed_commitment}', ${now - 24 * HOUR}
       )`,
    ]);
  }

  await executeScript([
    `UPDATE giveaways SET server_seed = '${pastSeed}', seed_commitment = '${manifest.seed_commitment}', locked_at = ${now - 24 * HOUR} WHERE id = '${pastId}'`,
    `UPDATE giveaway_stats SET participant_count = ${pastEntries.length}, entry_count = ${pastEntries.length}, winner_count = ${pastWinners.length}, updated_at = ${now} WHERE giveaway_id = '${pastId}'`,
    `INSERT INTO audit_log (guild_id, giveaway_id, action, actor_id, source, outcome, after_json, created_at)
     VALUES ('${guildId}', '${pastId}', 'giveaway.drawn', NULL, 'scheduler', 'success', '{"round":1,"winners":${manifest.winner_count}}', ${now - 24 * HOUR})`,
    `INSERT INTO giveaway_events (giveaway_id, guild_id, type, payload_json, created_at)
     VALUES ('${pastId}', '${guildId}', 'giveaway.drawn', '{"round":1,"winner_count":${manifest.winner_count},"participant_count":${manifest.participant_count}}', ${now - 24 * HOUR})`,
  ]);
  log("draw created", `winner ${pastWinners[0]?.user_id} (verifiable)`);

  // Verify the exact queries the UI runs.
  console.log("\nVerifying UI queries");
  console.log("-".repeat(60));

  const publicRow = await first(
    `SELECT g.id, g.guild_id, gu.name AS guild_name, g.title, g.status,
            COALESCE(s.participant_count, 0) AS participant_count,
            g.required_role_ids, g.required_mode, g.blacklist_role_ids,
            g.allowed_channel_ids, g.message_count_channel_ids, g.min_messages,
            g.entrants_require_membership, g.seed_commitment
       FROM giveaways g
       LEFT JOIN giveaway_stats s ON s.giveaway_id = g.id
       LEFT JOIN guilds gu ON gu.id = g.guild_id
      WHERE g.id = ?`,
    [giveawayId],
  );
  if (!publicRow) throw new Error("public giveaway query returned nothing");
  log("getPublicGiveaway");

  const active = await first<{ id: string; title: string; status: string }>(
    `SELECT g.id, g.title, g.status FROM giveaways g
      WHERE g.guild_id = ? AND g.status IN ('scheduled','running','paused')
      ORDER BY g.created_at DESC LIMIT 1`,
    [guildId],
  );
  if (!active) throw new Error("find_active returned nothing");
  log("find_active", active.title);

  const participants = await all(
    `SELECT e.user_id, COUNT(*) AS entries, MAX(e.joined_at) AS last_joined_at,
            c.message_count
       FROM giveaway_entries e
       LEFT JOIN message_counters c
              ON c.guild_id = (SELECT guild_id FROM giveaways WHERE id = e.giveaway_id)
             AND c.user_id = e.user_id
      WHERE e.giveaway_id = ?
      GROUP BY e.user_id
      ORDER BY MAX(e.joined_at) DESC
      LIMIT 25`,
    [giveawayId],
  );
  if (participants.length === 0) throw new Error("listParticipants returned nothing");
  log("listParticipants", `${participants.length} rows`);

  const analytics = await first(
    `SELECT COUNT(*) AS total,
            SUM(CASE WHEN status = 'running' THEN 1 ELSE 0 END) AS running
       FROM giveaways WHERE guild_id = ?`,
    [guildId],
  );
  if (!analytics) throw new Error("analytics query returned nothing");
  log("getAnalytics", `${analytics.total} giveaways`);

  const audit = await all(
    `SELECT id, action, actor_id, source, outcome, created_at
       FROM audit_log WHERE giveaway_id = ? ORDER BY id DESC LIMIT 30`,
    [giveawayId],
  );
  log("listAudit", `${audit.length} rows`);

  const events = await all(
    `SELECT id, type, payload_json, created_at FROM giveaway_events
      WHERE giveaway_id = ? AND id > ? ORDER BY id ASC LIMIT 50`,
    [giveawayId, 0],
  );
  log("listEventsSince", `${events.length} events`);

  console.log("\nSeed complete.");
  console.log(`  running: ${giveawayId}`);
  console.log(`  ended:   ${pastId}`);
  console.log(`\nRun \`npm run dev\` and open http://localhost:3000/giveaways`);
  console.log("The admin panel needs real Discord OAuth2 credentials.");
}

main().catch((error) => {
  console.error(`\nSeed failed: ${error instanceof Error ? error.message : String(error)}`);
  process.exit(1);
});
