-- 0006_participant_role.sql -- temporary "entrants" role
--
-- Why this exists
-- ---------------
-- Staff need to reach everyone who entered (reminders, winner announcements,
-- prize delivery) without pasting a long user list. Each entrant is given a
-- temporary role on join and it is removed when the giveaway ends.
--
-- Design notes
-- ------------
-- * `grant_source` records HOW the role came to be on the member:
--     'bot'      - the bot added it after a successful entry (we own it, and we
--                  remove it again on giveaway end / leave / disqualification)
--     'manual'   - a human assigned it; the bot never removes it
--   This matters: blindly stripping a role would delete permissions a human
--   granted on purpose.
-- * `participant_role_id` lives on the giveaway so history stays accurate even
--   if the role is later renamed or re-created, and so an old giveaway never
--   strips a role from the current one.
-- * The role is per-guild infrastructure (see guilds.participant_role_id) and is
--   reused across giveaways rather than created per giveaway.

-- Durable queue for temporary-role grants/revokes. Discord operations are not
-- transactional with our SQL, so they are journalled here and retried until they
-- succeed. Without this, a crash between "entry recorded" and "role added" (or
-- "giveaway ended" and "role removed") would silently strand members.
CREATE TABLE IF NOT EXISTS giveaway_role_tasks (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  giveaway_id TEXT NOT NULL,
  guild_id    TEXT NOT NULL,
  user_id     TEXT NOT NULL,
  -- Which role to act on, so a queued task survives the giveaway row being
  -- updated or the role being renamed.
  role_id     TEXT,
  action      TEXT NOT NULL CHECK (action IN ('add','remove')),
  status      TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending','claimed','done','failed')),
  attempts    INTEGER NOT NULL DEFAULT 0,
  last_error  TEXT,
  created_at  INTEGER NOT NULL,
  FOREIGN KEY (giveaway_id) REFERENCES giveaways(id) ON DELETE CASCADE
);
; statement-breakpoint
; statement-breakpoint
CREATE INDEX IF NOT EXISTS idx_role_tasks_claim ON giveaway_role_tasks (status, id);
; statement-breakpoint
; statement-breakpoint
-- One task per (giveaway, user, action): a duplicate enqueue is a no-op rather
-- than a redundant Discord call, and a retried grant cannot double-apply.
CREATE UNIQUE INDEX IF NOT EXISTS idx_role_tasks_unique
  ON giveaway_role_tasks (giveaway_id, user_id, action);
; statement-breakpoint
; statement-breakpoint
CREATE INDEX IF NOT EXISTS idx_role_tasks_pending ON giveaway_role_tasks (giveaway_id, status);
; statement-breakpoint

ALTER TABLE guilds ADD COLUMN participant_role_id TEXT;
; statement-breakpoint
; statement-breakpoint
ALTER TABLE giveaways ADD COLUMN participant_role_id TEXT;
; statement-breakpoint
; statement-breakpoint
-- Per-entry grant provenance, so a revoke only ever removes what we added.
ALTER TABLE giveaway_entries ADD COLUMN role_granted_at INTEGER;
; statement-breakpoint
; statement-breakpoint
ALTER TABLE giveaway_entries ADD COLUMN grant_source TEXT NOT NULL DEFAULT 'bot';
; statement-breakpoint
; statement-breakpoint
-- Reconciliation queries: who currently needs the role added or removed.
CREATE INDEX IF NOT EXISTS idx_entries_role_grants
  ON giveaway_entries (giveaway_id, grant_source, status);
; statement-breakpoint
; statement-breakpoint
CREATE INDEX IF NOT EXISTS idx_entries_user_grants
  ON giveaway_entries (giveaway_id, user_id, grant_source);
; statement-breakpoint
