/**
 * Guard used by every admin mutation.
 *
 * Checks, in order:
 *  1. a valid session exists
 *  2. the request came from our own origin (CSRF)
 *  3. the caller administers the target guild (RBAC)
 *  4. the action is within the rate limit
 *
 * Not marked "use server": only the exported action functions may be Server
 * Actions, and this file exports a class too.
 */

import { headers } from "next/headers";

import { requireGuildAdmin } from "@/lib/auth";
import { checkOrigin, rateLimitForUser } from "@/lib/security";
import { readSession } from "@/lib/session";

export interface ActionContext {
  userId: string;
  username: string;
}

export class ActionError extends Error {
  readonly status: number;
  constructor(message: string, status = 400) {
    super(message);
    this.name = "ActionError";
    this.status = status;
  }
}

export async function guard(action: string, guildId: string): Promise<ActionContext> {
  const headerList = await headers();

  const origin = checkOrigin(headerList);
  if (!origin.ok) {
    // 403 for every origin failure: never reveal which check tripped.
    console.warn(`[csrf] rejected ${action} from ${headerList.get("origin")}: ${origin.reason}`);
    throw new ActionError("Request rejected. Reload the page and try again.", 403);
  }

  const user = await readSession();
  if (!user) throw new ActionError("You need to sign in with Discord first.", 401);

  const limit = await rateLimitForUser(action);
  if (!limit.allowed) {
    throw new ActionError(`Too many ${action} requests. Try again in ${limit.resetSeconds}s.`, 429);
  }

  const auth = await requireGuildAdmin(guildId);
  if (!auth.allowed) {
    throw new ActionError(
      auth.reason === "not_signed_in"
        ? "You need to sign in with Discord first."
        : "You need Manage Server permission in this server.",
      403,
    );
  }

  return { userId: user.id, username: user.globalName ?? user.username };
}