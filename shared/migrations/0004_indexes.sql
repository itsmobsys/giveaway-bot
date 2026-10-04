-- 0004_indexes.sql -- hot-path indexes
--
-- Every index below backs a query that actually runs on a hot giveaway:
-- queue polling, public listing, participant pages, audit tails, live feeds.

CREATE INDEX IF NOT EXISTS idx_giveaways_guild_status  ON giveaways (guild_id, status, ends_at);
; statement-breakpoint
; statement-breakpoint
CREATE INDEX IF NOT EXISTS idx_giveaways_status_ends    ON giveaways (status, ends_at);
; statement-breakpoint
; statement-breakpoint
CREATE INDEX IF NOT EXISTS idx_giveaways_public        ON giveaways (status, ends_at, created_at DESC);
; statement-breakpoint
; statement-breakpoint
CREATE INDEX IF NOT EXISTS idx_giveaways_guild_channel ON giveaways (guild_id, channel_id);
; statement-breakpoint
; statement-breakpoint
CREATE INDEX IF NOT EXISTS idx_giveaways_message       ON giveaways (guild_id, message_id);
; statement-breakpoint
; statement-breakpoint
CREATE INDEX IF NOT EXISTS idx_entries_giveaway        ON giveaway_entries (giveaway_id, status, joined_at);
; statement-breakpoint
; statement-breakpoint
CREATE INDEX IF NOT EXISTS idx_entries_giveaway_user   ON giveaway_entries (giveaway_id, user_id);
; statement-breakpoint
; statement-breakpoint
CREATE INDEX IF NOT EXISTS idx_entries_user            ON giveaway_entries (user_id, giveaway_id);
; statement-breakpoint
; statement-breakpoint
CREATE INDEX IF NOT EXISTS idx_winners_giveaway        ON giveaway_winners (giveaway_id, round, rank);
; statement-breakpoint
; statement-breakpoint
CREATE INDEX IF NOT EXISTS idx_winners_user            ON giveaway_winners (user_id, awarded_at DESC);
; statement-breakpoint
; statement-breakpoint
CREATE INDEX IF NOT EXISTS idx_draws_giveaway          ON giveaway_draws (giveaway_id, round DESC);
; statement-breakpoint

CREATE INDEX IF NOT EXISTS idx_queue_claim             ON command_queue (status, priority, id);
; statement-breakpoint
; statement-breakpoint
CREATE INDEX IF NOT EXISTS idx_queue_giveaway          ON command_queue (giveaway_id, status);
; statement-breakpoint
; statement-breakpoint
CREATE INDEX IF NOT EXISTS idx_queue_created           ON command_queue (created_at);
; statement-breakpoint

CREATE INDEX IF NOT EXISTS idx_audit_guild_time        ON audit_log (guild_id, created_at DESC);
; statement-breakpoint
; statement-breakpoint
CREATE INDEX IF NOT EXISTS idx_audit_giveaway          ON audit_log (giveaway_id, created_at DESC);
; statement-breakpoint
; statement-breakpoint
CREATE INDEX IF NOT EXISTS idx_audit_actor             ON audit_log (actor_id, created_at DESC);
; statement-breakpoint

CREATE INDEX IF NOT EXISTS idx_events_giveaway         ON giveaway_events (giveaway_id, id DESC);
; statement-breakpoint
; statement-breakpoint
CREATE INDEX IF NOT EXISTS idx_events_guild            ON giveaway_events (guild_id, id DESC);
; statement-breakpoint
; statement-breakpoint
CREATE INDEX IF NOT EXISTS idx_admins_user             ON guild_admins (user_id);
; statement-breakpoint
; statement-breakpoint
CREATE INDEX IF NOT EXISTS idx_rate_limits_window      ON rate_limits (window_start);
; statement-breakpoint
; statement-breakpoint
CREATE INDEX IF NOT EXISTS idx_oauth_expiry            ON oauth_states (expires_at);
; statement-breakpoint
