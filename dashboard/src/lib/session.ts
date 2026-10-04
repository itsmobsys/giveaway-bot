/**
 * Signed, encrypted session cookie.
 *
 * Uses `jose` (JWE, AES-256-GCM) rather than a plaintext JWT so the session
 * payload is not readable from the cookie itself.
 *
 * Security properties:
 *  - `httpOnly` + `secure` + `sameSite=lax`: not readable from JS, not sent
 *    cross-site on top-level navigations
 *  - short, validated expiry enforced by `jose`
 *  - the session carries no secrets the client needs, only identity
 */

import { cookies } from "next/headers";
import { EncryptJWT, jwtDecrypt } from "jose";
import { createHash } from "node:crypto";

const COOKIE_NAME = "gw_session";
const ISSUER = "giveaway-dashboard";
const AUDIENCE = "giveaway-dashboard-web";

export interface SessionUser {
  id: string;
  username: string;
  globalName: string | null;
  avatar: string | null;
}

function maxAgeSeconds(): number {
  const raw = Number(process.env.SESSION_MAX_AGE_SECONDS ?? 604_800);
  return Number.isFinite(raw) && raw > 0 ? Math.floor(raw) : 604_800;
}

function secretKey(): Uint8Array | null {
  const secret = process.env.SESSION_SECRET;
  // 32 bytes exactly, required by A256GCM. Derive via SHA-256 so long
  // secrets keep their full entropy and non-ASCII secrets still key correctly.
  if (!secret || secret.length < 32) return null;
  return new Uint8Array(createHash("sha256").update(secret, "utf8").digest());
}

export function sessionsAvailable(): boolean {
  return secretKey() !== null;
}

export async function createSession(user: SessionUser): Promise<void> {
  const key = secretKey();
  if (!key) {
    throw new Error("SESSION_SECRET is missing or shorter than 32 characters");
  }
  const maxAge = maxAgeSeconds();
  const token = await new EncryptJWT({
    username: user.username,
    globalName: user.globalName,
    avatar: user.avatar,
  })
    .setProtectedHeader({ alg: "HS256", enc: "A256GCM" })
    .setSubject(user.id)
    .setIssuer(ISSUER)
    .setAudience(AUDIENCE)
    .setIssuedAt()
    .setExpirationTime(`${maxAge}s`)
    .encrypt(key);

  const store = await cookies();
  store.set(COOKIE_NAME, token, {
    httpOnly: true,
    secure: process.env.NODE_ENV === "production",
    sameSite: "lax",
    path: "/",
    maxAge,
  });
}

export async function readSession(): Promise<SessionUser | null> {
  const key = secretKey();
  if (!key) return null;
  const store = await cookies();
  const token = store.get(COOKIE_NAME)?.value;
  if (!token) return null;

  try {
    const { payload } = await jwtDecrypt(token, key, {
      issuer: ISSUER,
      audience: AUDIENCE,
    });
    const id = payload.sub;
    if (!id || typeof id !== "string" || !/^\d{15,25}$/.test(id)) return null;
    return {
      id,
      username: typeof payload.username === "string" ? payload.username : id,
      globalName: typeof payload.globalName === "string" ? payload.globalName : null,
      avatar: typeof payload.avatar === "string" ? payload.avatar : null,
    };
  } catch {
    // Expired, tampered with, or signed with an old secret: treat as signed out.
    return null;
  }
}

export async function destroySession(): Promise<void> {
  const store = await cookies();
  store.delete(COOKIE_NAME);
}