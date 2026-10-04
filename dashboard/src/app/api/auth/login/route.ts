import { safeInternalPath } from "@/lib/security";
import { all, first, nowMs, run } from "@/lib/db.ts";
import { readSession } from "@/lib/session.ts";

/**
 * Start Discord OAuth2.
 *
 * The `state` value is a random nonce stored in the database with a short TTL
 * and consumed exactly once - this is the CSRF protection for the login leg.
 * Without it an attacker could feed their own authorization code to the
 * callback and have the victim's browser adopt the attacker's account.
 */

import { NextResponse } from "next/server";
import { randomBytes } from "node:crypto";

import { buildAuthorizeUrl, discordConfigured } from "@/lib/auth";

export const dynamic = "force-dynamic";

export async function GET(request: Request): Promise<NextResponse> {
  const url = new URL(request.url);
  const redirectTo = url.searchParams.get("redirect_to") ?? "/admin";
  // Only allow same-site relative paths, so this cannot be used as an open redirect.
  const safeRedirect = safeInternalPath(redirectTo);

  if (!discordConfigured()) {
    return NextResponse.json(
      { error: "Discord OAuth2 is not configured on this deployment." },
      { status: 503 },
    );
  }

  const state = randomBytes(32).toString("base64url");
  const expiresAt = nowMs() + 10 * 60_000;

  await run("INSERT INTO oauth_states (state, redirect_to, created_at, expires_at) VALUES (?, ?, ?, ?)", [
    state,
    safeRedirect,
    nowMs(),
    expiresAt,
  ]);

  // Opportunistic cleanup of expired states; failure here must not block login.
  await run("DELETE FROM oauth_states WHERE expires_at < ?", [nowMs() - 3_600_000]).catch(
    () => undefined,
  );

  void readSession;
  void first;
  void all;

  return NextResponse.redirect(await buildAuthorizeUrl(state));
}
