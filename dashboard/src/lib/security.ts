/**
 * CSRF protection and shared rate limiting.
 *
 * CSRF
 * ----
 * Next.js Server Actions carry a `Next-Action` header and are same-origin POSTs,
 * which blocks classic cross-site form posts. This adds explicit defence:
 *  - origin checking against an allowlist (and the deployment's own origin)
 *  - a double-submit token bound to the session for anything extra sensitive
 *
 * Rate limiting
 * -------------
 * Backed by the shared `rate_limits` table so limits hold across serverless
 * instances and across the dashboard and the bot (both write to the same DB).
 * Fixed-window, incremented with a single UPSERT.
 */

import { randomBytes, timingSafeEqual } from "node:crypto";

import { nowMs, run } from "./db";
import { readSession } from "./session";

/** Cookie holding the CSRF token; readable by the browser so JS can echo it. */
const CSRF_COOKIE = "gw_csrf";
const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);

function expectedOrigins(): Set<string> {
  const origins = new Set<string>();
  const configured = process.env.CSRF_TRUSTED_ORIGINS;
  if (configured) {
    for (const value of configured.split(",")) {
      const trimmed = value.trim();
      if (trimmed) origins.add(trimmed);
    }
  }
  const appUrl = process.env.NEXT_PUBLIC_APP_URL?.trim();
  if (appUrl) origins.add(appUrl);
  // Vercel and Render both set this automatically.
  const vercel = process.env.VERCEL_URL?.trim();
  if (vercel) origins.add(`https://${vercel}`);
  const render = process.env.RENDER_EXTERNAL_URL?.trim();
  if (render) origins.add(render);
  return origins;
}

/** Verify a mutating request came from our own origin. */
export function checkOrigin(headers: Headers): { ok: boolean; reason?: string } {
  const origin = headers.get("origin");
  // Same-origin navigations may omit Origin on some browsers; fall back to Referer.
  const referer = headers.get("referer");
  const candidate = origin ?? referer;
  if (!candidate) {
    return { ok: false, reason: "missing_origin" };
  }

  let originUrl: URL;
  try {
    originUrl = new URL(candidate);
  } catch {
    return { ok: false, reason: "malformed_origin" };
  }

  if (SAFE_METHODS.has(originUrl.protocol.toUpperCase())) {
    return { ok: false, reason: "unexpected_method" };
  }

  const allowed = expectedOrigins();
  if (allowed.size === 0) {
    // No allowlist configured: fail closed rather than accept anything.
    return { ok: false, reason: "no_trusted_origins_configured" };
  }
  if (allowed.has(originUrl.origin)) return { ok: true };

  return { ok: false, reason: "origin_not_allowed" };
}

/** Issue (or reuse) a CSRF token bound to the signed-in user. */
export async function ensureCsrfToken(): Promise<string> {
  const user = await readSession();
  if (!user) return "";
  const { cookies } = await import("next/headers");
  const store = await cookies();
  const existing = store.get(CSRF_COOKIE)?.value;
  if (existing && existing.length >= 32) return existing;

  const token = randomBytes(24).toString("base64url");
  store.set(CSRF_COOKIE, token, {
    httpOnly: false, // the browser must be able to read and echo this
    secure: process.env.NODE_ENV === "production",
    sameSite: "lax",
    path: "/",
    maxAge: 60 * 60 * 24,
  });
  return token;
}

/** Constant-time comparison of a submitted token against the cookie. */
export async function verifyCsrfToken(submitted: string | undefined): Promise<boolean> {
  if (!submitted) return false;
  const { cookies } = await import("next/headers");
  const store = await cookies();
  const expected = store.get(CSRF_COOKIE)?.value;
  if (!expected) return false;

  const a = Buffer.from(submitted);
  const b = Buffer.from(expected);
  if (a.length !== b.length) return false;
  return timingSafeEqual(a, b);
}

export interface RateLimitResult {
  allowed: boolean;
  remaining: number;
  resetSeconds: number;
}

/** Fixed-window limiter, shared across instances via the database. */
export async function rateLimit(
  bucket: string,
  limit: number,
  windowSeconds: number,
): Promise<RateLimitResult> {
  const windowSecondsSafe = Math.max(1, Math.floor(windowSeconds));
  const max = limit > 0 ? Math.floor(limit) : 30;
  const windowStart = Math.floor(nowMs() / 1000 / windowSecondsSafe) * windowSecondsSafe;

  await run(
    `INSERT INTO rate_limits (bucket, window_start, hits) VALUES (?, ?, 1)
     ON CONFLICT(bucket, window_start) DO UPDATE SET hits = hits + 1`,
    [bucket, windowStart],
  );

  const row = await import("./db").then((m) =>
    m.first<{ hits: number }>(
      "SELECT hits FROM rate_limits WHERE bucket = ? AND window_start = ?",
      [bucket, windowStart],
    ),
  );
  const hits = Number(row?.hits ?? 1);
  const resetSeconds = Math.max(
    0,
    Math.ceil(((windowStart + windowSecondsSafe) * 1000 - nowMs()) / 1000),
  );
  return { allowed: hits <= max, remaining: Math.max(0, max - hits), resetSeconds };
}

export async function rateLimitForUser(action: string): Promise<RateLimitResult> {
  const user = await readSession();
  if (!user) {
    return { allowed: false, remaining: 0, resetSeconds: 60 };
  }
  const max = Number(process.env.RATE_LIMIT_MAX ?? 30);
  const window = Number(process.env.RATE_LIMIT_WINDOW_SECONDS ?? 60);
  return await rateLimit(`action:${action}:${user.id}`, max, window);
}
