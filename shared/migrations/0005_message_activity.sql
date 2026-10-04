-- 0005_message_activity.sql -- per-user message counters
--
-- Efficiency model
-- ----------------
-- One row per (guild, user). Counts are incremented in place with an UPSERT, so
-- tracking thousands of users costs one row each and no per-message history.
-- There is deliberately no per-message table: that would grow unboundedly.
--
-- Reliability model
-- -----------------
-- Counting is driven by the gateway. A gateway disconnect can drop events, so
-- each (guild, channel) pair keeps a monotonic `last_message_id`. On reconnect
-- the bot can backfill by fetching messages with id > last_message_id, which
-- makes counts converge instead of silently drifting. `window_started_at`
-- resets the counter when the tracking window is rolled.
--
-- `exactness` records how much we trust the number:
--   'exact'      - gateway seen every message in the window
--   'backfilled' - gaps were repaired from the REST API
--   'estimated'  - count is a lower bound (pruned or partially backfilled)

CREATE TABLE IF NOT EXISTS message_counters (
  guild_id         TEXT NOT NULL,
  user_id          TEXT NOT NULL,
  message_count    INTEGER NOT NULL DEFAULT 0,
  distinct_channels INTEGER NOT NULL DEFAULT 0,
  first_message_at INTEGER,
  last_message_at  INTEGER,
  window_started_at INTEGER NOT NULL,
  exactness        TEXT NOT NULL DEFAULT 'exact'
                     CHECK (exactness IN ('exact','backfilled','estimated')),
  updated_at       INTEGER NOT NULL,
  PRIMARY KEY (guild_id, user_id)
);
; statement-breakpoint
; statement-breakpoint

-- Per-user, per-channel counts. Kept deliberately small: only channels that a
-- running giveaway actually watches are recorded, so the row count is bounded
-- by (watched channels x active users) rather than by total message volume.
-- This is what makes a *channel-scoped* requirement exact instead of an
-- approximation of the guild-wide counter.
CREATE TABLE IF NOT EXISTS message_counter_channels (
  guild_id      TEXT NOT NULL,
  user_id       TEXT NOT NULL,
  channel_id    TEXT NOT NULL,
  message_count INTEGER NOT NULL DEFAULT 0,
  last_message_at INTEGER,
  updated_at    INTEGER NOT NULL,
  PRIMARY KEY (guild_id, user_id, channel_id)
);
; statement-breakpoint
; statement-breakpoint

-- Per-channel high-water marks used for gap detection and backfill.
CREATE TABLE IF NOT EXISTS message_channel_state (
  guild_id         TEXT NOT NULL,
  channel_id       TEXT NOT NULL,
  last_message_id  TEXT NOT NULL,
  last_message_at  INTEGER,
  message_count    INTEGER NOT NULL DEFAULT 0,
  updated_at       INTEGER NOT NULL,
  PRIMARY KEY (guild_id, channel_id)
);
; statement-breakpoint
; statement-breakpoint

-- Giveaway columns (see below) are added here rather than to 0001 because
-- migrations are immutable once applied.
ALTER TABLE giveaways ADD COLUMN min_messages INTEGER NOT NULL DEFAULT 0;
; statement-breakpoint
; statement-breakpoint
ALTER TABLE giveaways ADD COLUMN message_count_channel_ids TEXT NOT NULL DEFAULT '[]';
; statement-breakpoint
; statement-breakpoint
ALTER TABLE giveaways ADD COLUMN message_count_ignore_bots INTEGER NOT NULL DEFAULT 1;
; statement-breakpoint
; statement-breakpoint
-- When 1, only messages sent after this timestamp count (a clean-slate rule
-- that stops a long-time member grandfathering in past activity).
ALTER TABLE giveaways ADD COLUMN message_count_since INTEGER;
; statement-breakpoint
; statement-breakpoint
-- Which guild scope to use for counting. 'guild' counts the whole server,
-- 'channel' counts only the channels listed above.
ALTER TABLE giveaways ADD COLUMN message_count_scope TEXT NOT NULL DEFAULT 'guild'
  CHECK (message_count_scope IN ('guild','channel'));
; statement-breakpoint

-- Hot path: eligibility checks read this for one user.
CREATE INDEX IF NOT EXISTS idx_msg_counters_user ON message_counters (user_id, guild_id);
; statement-breakpoint
; statement-breakpoint
-- Channel-scoped eligibility: one index serves both the per-user sum and the
-- per-channel leaderboard.
CREATE INDEX IF NOT EXISTS idx_msg_channel_user ON message_counter_channels (guild_id, user_id);
; statement-breakpoint
; statement-breakpoint
CREATE INDEX IF NOT EXISTS idx_msg_channel_rank ON message_counter_channels (guild_id, channel_id, message_count DESC);
; statement-breakpoint
; statement-breakpoint
-- Leaderboards / "top chatters" on the dashboard.
CREATE INDEX IF NOT EXISTS idx_msg_counters_rank ON message_counters (guild_id, message_count DESC);
; statement-breakpoint
; statement-breakpoint
-- Giveaways that actually have the requirement enabled.
CREATE INDEX IF NOT EXISTS idx_giveaways_message_req ON giveaways (guild_id, min_messages);
; statement-breakpoint
