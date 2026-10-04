-- 0002_entries_draws.sql -- entries, draws, winners
-- Every row here is part of the public audit surface.

CREATE TABLE IF NOT EXISTS giveaway_entries (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  giveaway_id         TEXT NOT NULL,
  user_id             TEXT NOT NULL,
  entry_seq           INTEGER NOT NULL,          -- this user's n-th entry (1..max_entries_per_user)
  account_created_at  INTEGER,                   -- snowflake -> epoch ms, validated at join time
  guild_joined_at     INTEGER,
  status              TEXT NOT NULL DEFAULT 'valid'
                        CHECK (status IN ('valid','invalid','disqualified','winner','lost')),
  invalid_reason      TEXT,
  removed_by          TEXT,
  removed_at          INTEGER,
  removed_reason      TEXT,
  snapshot_json       TEXT,                       -- eligibility snapshot taken at join time
  joined_at           INTEGER NOT NULL,
  updated_at          INTEGER NOT NULL,
  UNIQUE (giveaway_id, user_id, entry_seq),
  FOREIGN KEY (giveaway_id) REFERENCES giveaways(id) ON DELETE CASCADE
);
; statement-breakpoint

-- One row per draw attempt (initial draw + every reroll). Written in the same
-- transaction as the winners, after the participant set is frozen.
CREATE TABLE IF NOT EXISTS giveaway_draws (
  id                TEXT PRIMARY KEY,
  giveaway_id       TEXT NOT NULL,
  round             INTEGER NOT NULL,
  method            TEXT NOT NULL DEFAULT 'hmac-sha256-commit-reveal',
  algorithm_version TEXT NOT NULL,
  server_seed       TEXT NOT NULL,               -- revealed hex (public from now on)
  seed_commitment   TEXT NOT NULL,               -- sha256(server_seed), published at giveaway creation
  participant_digest TEXT NOT NULL,              -- sha256 of the frozen, sorted entry list
  participant_count INTEGER NOT NULL,
  eligible_count    INTEGER NOT NULL,
  winner_count      INTEGER NOT NULL,
  manifest_json     TEXT NOT NULL,               -- full per-entry derived scores
  triggered_by      TEXT,
  trigger_reason    TEXT NOT NULL,               -- ended | reroll | admin_draw
  duration_ms       INTEGER,
  created_at        INTEGER NOT NULL,
  UNIQUE (giveaway_id, round),
  FOREIGN KEY (giveaway_id) REFERENCES giveaways(id) ON DELETE CASCADE
);
; statement-breakpoint

CREATE TABLE IF NOT EXISTS giveaway_winners (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  giveaway_id   TEXT NOT NULL,
  draw_id       TEXT NOT NULL,
  round         INTEGER NOT NULL,
  user_id       TEXT NOT NULL,
  rank          INTEGER NOT NULL,
  entry_id      INTEGER,
  entry_seq     INTEGER,
  score         TEXT NOT NULL,                   -- decimal string of the unbiased score
  server_seed   TEXT NOT NULL,
  seed_commitment TEXT NOT NULL,
  awarded_at    INTEGER NOT NULL,
  FOREIGN KEY (giveaway_id) REFERENCES giveaways(id) ON DELETE CASCADE,
  FOREIGN KEY (draw_id) REFERENCES giveaway_draws(id) ON DELETE CASCADE
);
; statement-breakpoint

-- Denormalised public counter so the dashboard never has to COUNT(*) a hot table.
CREATE TABLE IF NOT EXISTS giveaway_stats (
  giveaway_id       TEXT PRIMARY KEY,
  participant_count INTEGER NOT NULL DEFAULT 0,
  entry_count      INTEGER NOT NULL DEFAULT 0,
  winner_count     INTEGER NOT NULL DEFAULT 0,
  updated_at       INTEGER NOT NULL,
  FOREIGN KEY (giveaway_id) REFERENCES giveaways(id) ON DELETE CASCADE
);