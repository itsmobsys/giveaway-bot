/**
 * Discord OAuth2 login and guild-administrator authorization.
 *
 * Trust model
 * ------------
 * The session proves *identity* only. Every privileged action additionally
 * requires the user to hold Manage Server (or Administrator) in the target
 * guild, verified from Discord's API and cached in `guild_admins`. Nothing is
 * authorised on the strength of a session alone.
 */

import { all, first, nowMs, run } from "./db";
import { readSession, sessionSecretLength, type SessionUser } from "./session";

/** Discord permission bits. */
export const PERM_ADMINISTRATOR = 0x00000008n;
export const PERM_MANAGE_GUILD = 0x00000020n;
export const ADMIN_MASK = PERM_ADMINISTRATOR | PERM_MANAGE_GUILD;

export interface GuildMembership {
  guildId: string;
  permissions: bigint;
  roles: string[];
  nickname: string | null;
}

export function isGuildAdmin(permissions: bigint): boolean {
  return (permissions & ADMIN_MASK) !== 0n;
}

function requireEnv(name: string): string {
  const value = process.env[name];
  if (!value) throw new Error(`${name} is not configured`);
  return value;
}

export function discordConfigured(): boolean {
  return Boolean(
    process.env.DISCORD_CLIENT_ID && process.env.DISCORD_CLIENT_SECRET && process.env.DISCORD_REDIRECT_URI,
  );
}

/**
 * Per-key configuration presence for the login page's setup checklist.
 *
 * Only booleans and the secret *length* leave this function — never values —
 * so it is safe to render to an unauthenticated visitor. The blanket "not
 * fully configured" message sent operators in circles ("but I set them!") when
 * exactly one key was at fault, usually a <32-char secret or a variable saved
 * to Preview while visiting Production.
 */
export interface AuthConfigStatus {
  sessionSecretLength: number;
  sessionSecretOk: boolean;
  clientId: boolean;
  /** Numeric snowflake like the Application ID. A pasted secret fails this. */
  clientIdValid: boolean;
  clientSecret: boolean;
  redirectUri: boolean;
  redirectIsLocalhost: boolean;
}

export function authConfigStatus(): AuthConfigStatus {
  const secretLength = sessionSecretLength();
  const redirectUri = process.env.DISCORD_REDIRECT_URI ?? "";
  const clientId = process.env.DISCORD_CLIENT_ID ?? "";
  return {
    sessionSecretLength: secretLength,
    sessionSecretOk: secretLength >= 32,
    clientId: Boolean(clientId),
    clientIdValid: /^\d{15,25}$/.test(clientId),
    clientSecret: Boolean(process.env.DISCORD_CLIENT_SECRET),
    redirectUri: Boolean(redirectUri),
    redirectIsLocalhost: /localhost|127\.0\.0\.1/i.test(redirectUri),
  };
}

export function botToken(): string | null {
  return process.env.DISCORD_BOT_TOKEN ?? null;
}

/** OAuth2 authorization URL with a random, single-use state. */
export async function buildAuthorizeUrl(state: string): Promise<string> {
  const params = new URLSearchParams({
    client_id: requireEnv("DISCORD_CLIENT_ID"),
    redirect_uri: requireEnv("DISCORD_REDIRECT_URI"),
    response_type: "code",
    scope: "identify guilds",
    state,
    prompt: "consent",
  });
  return `https://discord.com/oauth2/authorize?${params.toString()}`;
}

/**
 * Exchange an authorization code for an access token.
 *
 * The code is single-use and short-lived; we do not store the access token
 * because guild permission checks are done with the *bot* token below.
 */
export async function exchangeCode(code: string): Promise<string> {
  const response = await fetch("https://discord.com/api/v10/oauth2/token", {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      client_id: requireEnv("DISCORD_CLIENT_ID"),
      client_secret: requireEnv("DISCORD_CLIENT_SECRET"),
      grant_type: "authorization_code",
      code,
      redirect_uri: requireEnv("DISCORD_REDIRECT_URI"),
    }),
    cache: "no-store",
  });

  if (!response.ok) {
    throw new Error(`OAuth token exchange failed (${response.status})`);
  }
  const body = (await response.json()) as { access_token?: string };
  if (!body.access_token) throw new Error("OAuth response contained no access_token");
  return body.access_token;
}

/** Fetch the signed-in user's identity. */
export async function fetchIdentity(accessToken: string): Promise<SessionUser> {
  const response = await fetch("https://discord.com/api/v10/users/@me", {
    headers: { Authorization: `Bearer ${accessToken}` },
    cache: "no-store",
  });
  if (!response.ok) throw new Error(`Could not read your Discord profile (${response.status})`);

  const user = (await response.json()) as {
    id: string;
    username: string;
    global_name?: string | null;
    avatar?: string | null;
  };
  if (!/^\d{15,25}$/.test(user.id)) throw new Error("Discord returned an unexpected user id");

  return {
    id: user.id,
    username: user.username,
    globalName: user.global_name ?? null,
    avatar: user.avatar
      ? `https://cdn.discordapp.com/avatars/${user.id}/${user.avatar}.png?size=128`
      : null,
  };
}

/**
 * Read a member's permissions using the *bot* token.
 *
 * This is the authoritative check: the bot can see guild members, so
 * `GET /guilds/{guild}/members/{user}` returns the raw permission bitfield. It
 * needs the bot to be in the guild, which is exactly the right precondition.
 */
export async function fetchGuildMembership(
  guildId: string,
  userId: string,
): Promise<GuildMembership | null> {
  const token = botToken();
  if (!token) return null;

  const response = await fetch(
    `https://discord.com/api/v10/guilds/${guildId}/members/${userId}`,
    {
      headers: { Authorization: `Bot ${token}` },
      // Membership changes must be reflected promptly, so this is not cached
      // at the edge; we cache in the database below instead.
      cache: "no-store",
    },
  );

  if (response.status === 404) return null;
  if (!response.ok) {
    throw new Error(`Discord membership lookup failed (${response.status})`);
  }

  const member = (await response.json()) as {
    permissions?: string;
    roles?: string[];
    nick?: string | null;
  };

  return {
    guildId,
    permissions: BigInt(member.permissions ?? "0"),
    roles: member.roles ?? [],
    nickname: member.nick ?? null,
  };
}

export interface AdminCheck {
  allowed: boolean;
  permissions: bigint;
  reason?: string;
}

/**
 * Authorize an action against a guild.
 *
 * Order matters: cache first (fast path), then Discord (authoritative), then
 * re-check. A cached "allowed" is revalidated after the TTL so a demotion takes
 * effect within `CACHE_TTL_MS`.
 */
export async function requireGuildAdmin(guildId: string): Promise<AdminCheck> {
  const user = await readSession();
  if (!user) return { allowed: false, permissions: 0n, reason: "not_signed_in" };
  if (!/^\d{15,25}$/.test(guildId)) {
    return { allowed: false, permissions: 0n, reason: "invalid_guild" };
  }

  const CACHE_TTL_MS = 5 * 60_000;
  const cached = await first<{ permissions: string; synced_at: number }>(
    "SELECT permissions, synced_at FROM guild_admins WHERE guild_id = ? AND user_id = ?",
    [guildId, user.id],
  );

  if (cached && nowMs() - Number(cached.synced_at) < CACHE_TTL_MS) {
    const permissions = BigInt(cached.permissions);
    return isGuildAdmin(permissions)
      ? { allowed: true, permissions }
      : { allowed: false, permissions, reason: "not_an_admin" };
  }

  let membership: GuildMembership | null = null;
  try {
    membership = await fetchGuildMembership(guildId, user.id);
  } catch (error) {
    return {
      allowed: false,
      permissions: 0n,
      reason: error instanceof Error ? error.message : "membership_check_failed",
    };
  }

  if (!membership) {
    // The user is not in the guild, or the bot cannot see members there.
    return { allowed: false, permissions: 0n, reason: "not_a_member_or_bot_lacks_access" };
  }

  await run(
    `INSERT INTO guild_admins (guild_id, user_id, username, permissions, source, synced_at)
     VALUES (?, ?, ?, ?, 'rest', ?)
     ON CONFLICT(guild_id, user_id) DO UPDATE SET
       username = excluded.username,
       permissions = excluded.permissions,
       source = excluded.source,
       synced_at = excluded.synced_at`,
    [
      guildId,
      user.id,
      user.globalName ?? user.username,
      membership.permissions.toString(),
      nowMs(),
    ],
  );

  return isGuildAdmin(membership.permissions)
    ? { allowed: true, permissions: membership.permissions }
    : { allowed: false, permissions: membership.permissions, reason: "not_an_admin" };
}

/** Guilds where this user is an administrator and the bot is present. */
export async function listAdminGuilds(userId: string): Promise<AdminGuild[]> {
  // The guild_admins table is only a cache populated by requireGuildAdmin, so a
  // user who just signed in has zero rows and /admin shows "No servers found"
  // even when they administer several bot guilds. Sync first so the listing
  // reflects Discord reality instead of cache warmth.
  await syncAdminGuilds(userId);
  return await all<AdminGuild>(
    `SELECT g.id, g.name, g.icon_url, g.member_count
       FROM guild_admins a
       JOIN guilds g ON g.id = a.guild_id
      WHERE a.user_id = ?
        -- Filter on the admin bits, not "has any permission at all".
        -- requireGuildAdmin records a row for every member it can resolve, admin
        -- or not, storing their true permission bitfield, so a != 0 test matched
        -- member whose only permission was VIEW_CHANNEL. Their guild then appeared
        -- on /admin with its name, icon, member count and giveaway analytics, and
        -- they could read every giveaway, entry and winner in it.
        AND (CAST(a.permissions AS INTEGER) & 40) != 0
        AND g.bot_present = 1
      ORDER BY g.name COLLATE NOCASE`,
    [userId],
  );
}

/**
 * Reconcile the guild_admins cache for one user against Discord.
 *
 * Lists every guild where the bot is present, asks Discord (via the bot token)
 * what this user's permissions are there, and upserts the result. Unknown users
 * (not a member, or bot cannot see them) get a 0-permission row so the next
 * listing does not re-hit Discord for them until the TTL expires.
 */
export async function syncAdminGuilds(userId: string): Promise<void> {
  if (!/^\d{15,25}$/.test(userId)) return;
  if (!botToken()) return;
  let botGuilds: Array<{ id: string }>;
  try {
    botGuilds = await all<{ id: string }>(
      "SELECT id FROM guilds WHERE bot_present = 1 LIMIT 100",
    );
  } catch {
    return;
  }
  if (botGuilds.length === 0) return;

  // Only re-check guilds whose cache entry is missing or stale.
  const CACHE_TTL_MS = 5 * 60_000;
  const now = nowMs();
  for (const guild of botGuilds) {
    const cached = await first<{ permissions: string; synced_at: number }>(
      "SELECT permissions, synced_at FROM guild_admins WHERE guild_id = ? AND user_id = ?",
      [guild.id, userId],
    );
    if (cached && now - Number(cached.synced_at) < CACHE_TTL_MS) continue;
    let membership: GuildMembership | null = null;
    try {
      membership = await fetchGuildMembership(guild.id, userId);
    } catch (error) {
      // A transient Discord failure must not wipe a good cache entry: keep
      // the old row and try again next time.
      console.warn(`[auth] membership lookup failed for guild ${guild.id}:`, error);
      continue;
    }
    const permissions = membership ? membership.permissions.toString() : "0";
    const username = membership?.nickname ?? "";
    try {
      await run(
        `INSERT INTO guild_admins (guild_id, user_id, username, permissions, source, synced_at)
         VALUES (?, ?, ?, ?, 'rest', ?)
         ON CONFLICT(guild_id, user_id) DO UPDATE SET
           username = excluded.username,
           permissions = excluded.permissions,
           source = excluded.source,
           synced_at = excluded.synced_at`,
        [guild.id, userId, username, permissions, nowMs()],
      );
    } catch (error) {
      console.warn(`[auth] could not cache membership for guild ${guild.id}:`, error);
    }
  }
}

export interface AdminGuild {
  id: string;
  name: string;
  icon_url: string | null;
  member_count: number;
}

export type { SessionUser };


