/**
 * Discord OAuth2 callback.
 *
 * Security sequence:
 *  1. verify `state` exists, is unexpired and is unused (login CSRF)
 *  2. exchange the one-time code for an access token
 *  3. read the identity and set an httpOnly, secure, sameSite session cookie
 *  4. consume the state so it can never be replayed
 *
 * On any failure the user is redirected to /login with an error rather than
 * being shown a stack trace.
 */

import { NextResponse } from "next/server";

import { safeInternalPath } from "@/lib/security";
import { discordConfigured, exchangeCode, fetchIdentity } from "@/lib/auth";
import { all, run } from "@/lib/db";
import { createSession, sessionsAvailable } from "@/lib/session";

export const dynamic = "force-dynamic";

interface StateRow {
  state: string;
  redirect_to: string;
  expires_at: number;
  consumed_at: number | null;
}

export async function GET(request: Request): Promise<NextResponse> {
  const url = new URL(request.url);
  const code = url.searchParams.get("code");
  const state = url.searchParams.get("state");
  const error = url.searchParams.get("error");

  const loginUrl = (reason: string) =>
    NextResponse.redirect(new URL(`/login?error=${encodeURIComponent(reason)}`, url.origin));

  if (error) return loginUrl("discord_denied");
  if (!code || !state) return loginUrl("missing_code");

  if (!sessionsAvailable()) {
    console.error("SESSION_SECRET is not set or is too short");
    return loginUrl("server_misconfigured");
  }
  if (!discordConfigured()) {
    return loginUrl("oauth_not_configured");
  }

  // 1. Verify the state: must exist, be unexpired, and be unused.
  const rows = await all<StateRow>(
    "SELECT state, redirect_to, expires_at, consumed_at FROM oauth_states WHERE state = ?",
    [state],
  );
  const record = rows[0];
  if (!record || record.consumed_at !== null || Number(record.expires_at) < Date.now()) {
    return loginUrl("invalid_state");
  }

  try {
    // 2. Exchange the one-time code.
    const accessToken = await exchangeCode(code);

    // 3. Read identity and create the session.
    const identity = await fetchIdentity(accessToken);
    await createSession(identity);

    // 4. Consume the state so it cannot be replayed.
    await run("UPDATE oauth_states SET consumed_at = ? WHERE state = ?", [Date.now(), state]);

    const target = safeInternalPath(record.redirect_to);
    return NextResponse.redirect(new URL(target, url.origin));
  } catch (err) {
    console.error("OAuth callback failed:", err);
    return loginUrl("exchange_failed");
  }
}