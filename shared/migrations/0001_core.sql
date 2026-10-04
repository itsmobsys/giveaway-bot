-- 0001_core.sql -- guilds, admin cache, giveaways
-- Statement separator used by both runners: a line containing
--   ; statement-breakpoint
-- Portable across SQLite 3.x and Turso/libSQL.

CREATE TABLE IF NOT EXISTS schema_migrations (
  filename    TEXT PRIMARY KEY,
  checksum    TEXT NOT NULL,
  applied_at  INTEGER NOT NULL,
  duration_ms INTEGER NOT NULL DEFAULT 0
);
; statement-breakpoint

CREATE TABLE IF NOT EXISTS guilds (
  id           TEXT PRIMARY KEY,
  name         TEXT NOT NULL,
  icon_url     TEXT,
  owner_id     TEXT,
  member_count INTEGER NOT NULL DEFAULT 0,
  bot_present  INTEGER NOT NULL DEFAULT 0,
  synced_at    INTEGER,
  created_at   INTEGER NOT NULL,
  updated_at   INTEGER NOT NULL
);
; statement-breakpoint

-- Cached guild-administration state for the dashboard RBAC fast path.
-- Populated by the bot (members intent) and refreshed on demand from the
-- Discord REST API by the dashboard. `permissions` is the raw bitfield.
CREATE TABLE IF NOT EXISTS guild_admins (
  guild_id   TEXT NOT NULL,
  user_id    TEXT NOT NULL,
  username   TEXT,
  permissions INTEGER NOT NULL DEFAULT 0,
  source     TEXT NOT NULL DEFAULT 'rest',   -- rest | bot
  synced_at  INTEGER NOT NULL,
  PRIMARY KEY (guild_id, user_id)
);
; statement-breakpoint

CREATE TABLE IF NOT EXISTS giveaways (
  id                 TEXT PRIMARY KEY,
  guild_id           TEXT NOT NULL,
  channel_id         TEXT NOT NULL,
  message_id         TEXT,
  status             TEXT NOT NULL DEFAULT 'scheduled'
                       CHECK (status IN ('scheduled','running','paused','ended')),
  ended_reason       TEXT,
  title              TEXT NOT NULL,
  description        TEXT NOT NULL DEFAULT '',
  prize              TEXT NOT NULL DEFAULT '',
  prize_image_url    TEXT,
  prize_count        INTEGER NOT NULL DEFAULT 1,
  winner_count       INTEGER NOT NULL DEFAULT 1,
  entry_limit        INTEGER NOT NULL DEFAULT 0,        -- 0 = unlimited total entries
  max_entries_per_user INTEGER NOT NULL DEFAULT 1,      -- >= 1
  starts_at          INTEGER,
  ends_at            INTEGER,
  original_ends_at   INTEGER,
  paused_at          INTEGER,
  paused_remaining_ms INTEGER,
  total_draws        INTEGER NOT NULL DEFAULT 0,
  required_role_ids  TEXT NOT NULL DEFAULT '[]',         -- JSON array (whitelist)
  required_mode      TEXT NOT NULL DEFAULT 'any'
                       CHECK (required_mode IN ('any','all')),
  blacklist_role_ids TEXT NOT NULL DEFAULT '[]',         -- JSON array
  allowed_channel_ids TEXT NOT NULL DEFAULT '[]',        -- JSON array, [] = every channel
  min_account_age_days INTEGER NOT NULL DEFAULT 0,
  min_guild_join_days   INTEGER NOT NULL DEFAULT 0,
  entrants_require_membership INTEGER NOT NULL DEFAULT 1,
  -- Provably fair state. `server_seed` is written once when the giveaway is
  -- sealed (status -> running) and must not be read again until a draw runs.
  -- `seed_commitment` is public from that moment onwards.
  server_seed        TEXT,
  seed_commitment    TEXT,
  seed_sealed_at     INTEGER,
  seed_revealed_at   INTEGER,
  draw_round         INTEGER NOT NULL DEFAULT 0,
  locked_at          INTEGER,
  created_by         TEXT NOT NULL,
  updated_by         TEXT,
  version            INTEGER NOT NULL DEFAULT 1,
  created_at         INTEGER NOT NULL,
  updated_at         INTEGER NOT NULL,
  FOREIGN KEY (guild_id) REFERENCES guilds(id) ON DELETE CASCADE
);
; statement-breakpoint

CREATE TABLE IF NOT EXISTS bot_state (
  key        TEXT PRIMARY KEY,
  value      TEXT,
  updated_at INTEGER NOT NULL
);