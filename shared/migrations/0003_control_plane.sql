-- 0003_control_plane.sql -- command queue, audit log, events, oauth, rate limits

-- Dashboard -> bot command queue. The dashboard never talks to Discord; it
-- writes a row here and the bot claims it atomically.
CREATE TABLE IF NOT EXISTS command_queue (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  guild_id      TEXT NOT NULL,
  giveaway_id   TEXT,
  kind          TEXT NOT NULL,       -- giveaway.create | giveaway.pause | giveaway.extend | ...
  payload_json  TEXT NOT NULL,
  requested_by  TEXT NOT NULL,       -- discord user id
  requested_by_name TEXT,
  source        TEXT NOT NULL,       -- dashboard | discord | api
  status        TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending','claimed','succeeded','failed','cancelled')),
  priority      INTEGER NOT NULL DEFAULT 100,
  attempts      INTEGER NOT NULL DEFAULT 0,
  last_error    TEXT,
  result_json   TEXT,
  created_at    INTEGER NOT NULL,
  claimed_at    INTEGER,
  processed_at  INTEGER,
  FOREIGN KEY (guild_id) REFERENCES guilds(id) ON DELETE CASCADE
);
; statement-breakpoint

-- Append-only audit trail. `before_json`/`after_json` make every mutation
-- independently reviewable; nothing in the product can edit or delete rows.
CREATE TABLE IF NOT EXISTS audit_log (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  guild_id      TEXT NOT NULL,
  giveaway_id   TEXT,
  action        TEXT NOT NULL,        -- giveaway.created | giveaway.paused | entry.disqualified | ...
  actor_id      TEXT,
  actor_name    TEXT,
  source        TEXT NOT NULL,        -- dashboard | discord | api | scheduler
  target_id     TEXT,
  outcome       TEXT NOT NULL DEFAULT 'success',   -- success | denied | error
  before_json   TEXT,
  after_json    TEXT,
  metadata_json TEXT,
  ip_hash       TEXT,
  created_at    INTEGER NOT NULL,
  FOREIGN KEY (guild_id) REFERENCES guilds(id) ON DELETE CASCADE
);
; statement-breakpoint

-- Lightweight feed powering dashboard live status (polling / SSE).
CREATE TABLE IF NOT EXISTS giveaway_events (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  giveaway_id TEXT,
  guild_id    TEXT NOT NULL,
  type        TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at  INTEGER NOT NULL,
  FOREIGN KEY (guild_id) REFERENCES guilds(id) ON DELETE CASCADE
);
; statement-breakpoint

-- OAuth2 `state` values for login CSRF protection. Single use, short lived.
CREATE TABLE IF NOT EXISTS oauth_states (
  state       TEXT PRIMARY KEY,
  redirect_to TEXT,
  created_at  INTEGER NOT NULL,
  expires_at  INTEGER NOT NULL,
  consumed_at INTEGER
);
; statement-breakpoint

-- Shared, database-backed rate limiting (works across serverless instances).
CREATE TABLE IF NOT EXISTS rate_limits (
  bucket       TEXT NOT NULL,
  window_start INTEGER NOT NULL,
  hits         INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (bucket, window_start)
);