"use server";

/**
 * Admin Server Actions.
 *
 * These are the only paths from the dashboard to the bot, and every one of them
 * goes through `guard()` (session + CSRF + RBAC + rate limit) and validates its
 * input with zod before anything is written.
 *
 * The dashboard itself never touches Discord: it enqueues a command that the
 * Python bot executes after re-validating it independently.
 */

import { revalidatePath } from "next/cache";

import {
  cancelSchema,
  createSchema,
  endSchema,
  enqueueCommand,
  entrySchema,
  extendSchema,
  hasPendingCommand,
  messageRequirementSchema,
  revalidateSchema,
  rerollSchema,
  shortenSchema,
  updateSchema,
} from "@/lib/commands";
import { first } from "@/lib/db";
import { getActiveGiveaway } from "@/lib/giveaways";

import { ActionError, guard } from "./guard";

export interface ActionResult {
  ok: boolean;
  message: string;
  commandId?: number;
}

/** Consistent shape so the client can always render something useful. */
function fail(error: unknown): ActionResult {
  if (error instanceof ActionError) return { ok: false, message: error.message };
  if (error instanceof Error) {
    if (error.name === "ZodError") {
      const issues = (error as unknown as { issues?: Array<{ path: unknown[]; message: string }> })
        .issues;
      const message =
        issues && issues.length > 0
          ? issues.map((issue) => `${issue.path.join(".") || "value"}: ${issue.message}`).join("; ")
          : "Some values were not valid.";
      return { ok: false, message };
    }
    console.error("admin action failed:", error);
    return { ok: false, message: "Something went wrong. Please try again." };
  }
  return { ok: false, message: "Something went wrong. Please try again." };
}

async function enqueue(
  guildId: string,
  giveawayId: string | null,
  kind: Parameters<typeof enqueueCommand>[0]["kind"],
  payload: Record<string, unknown>,
  userId: string,
  username: string,
): Promise<number> {
  const { commandId } = await enqueueCommand({
    guildId,
    giveawayId,
    kind,
    payload,
    requestedBy: userId,
    requestedByName: username,
    source: "dashboard",
  });
  return commandId;
}

export async function createGiveawayAction(
  guildId: string,
  raw: unknown,
): Promise<ActionResult> {
  try {
    const user = await guard("create", guildId);

    // One giveaway at a time per server: this keeps the entrants role meaningful.
    const active = await getActiveGiveaway(guildId);
    if (active) {
      return {
        ok: false,
        message: `This server already has an active giveaway ("${active.title}"). End it before starting another.`,
      };
    }

    const data = createSchema.parse(raw);
    const commandId = await enqueue(guildId, null, "giveaway.create", data as never, user.userId, user.username);
    revalidatePath(`/admin/guilds/${guildId}`);
    return {
      ok: true,
      commandId,
      message: "Giveaway created. The bot will post it in a moment.",
    };
  } catch (error) {
    return fail(error);
  }
}

export async function updateGiveawayAction(
  guildId: string,
  giveawayId: string,
  raw: unknown,
): Promise<ActionResult> {
  try {
    const user = await guard("update", guildId);
    const data = updateSchema.parse(raw);
    const commandId = await enqueue(guildId, giveawayId, "giveaway.update", data as never, user.userId, user.username);
    revalidatePath(`/admin/guilds/${guildId}/giveaways/${giveawayId}`);
    return { ok: true, commandId, message: "Changes sent to the bot." };
  } catch (error) {
    return fail(error);
  }
}

/** Shared implementation for the simple lifecycle commands. */
async function lifecycle(
  guildId: string,
  giveawayId: string,
  kind: Parameters<typeof enqueueCommand>[0]["kind"],
  action: string,
  parse: (raw: unknown) => unknown,
  raw: unknown,
  message: string,
): Promise<ActionResult> {
  try {
    const user = await guard(action, guildId);
    const payload = parse(raw) as Record<string, unknown>;

    // Guard against stacking commands the bot has not processed yet.
    if (await hasPendingCommand(giveawayId)) {
      return {
        ok: false,
        message: "A previous command for this giveaway is still being processed. Try again in a moment.",
      };
    }

    const commandId = await enqueue(guildId, giveawayId, kind, payload, user.userId, user.username);
    revalidatePath(`/admin/guilds/${guildId}/giveaways/${giveawayId}`);
    return { ok: true, commandId, message };
  } catch (error) {
    return fail(error);
  }
}

export async function pauseGiveawayAction(
  guildId: string,
  giveawayId: string,
  raw: unknown,
): Promise<ActionResult> {
  return await lifecycle(
    guildId, giveawayId, "giveaway.pause", "pause",
    () => ({}), raw, "Giveaway paused.",
  );
}

export async function resumeGiveawayAction(
  guildId: string,
  giveawayId: string,
  raw: unknown,
): Promise<ActionResult> {
  return await lifecycle(
    guildId, giveawayId, "giveaway.resume", "resume",
    () => ({}), raw, "Giveaway resumed.",
  );
}

export async function extendGiveawayAction(
  guildId: string,
  giveawayId: string,
  raw: unknown,
): Promise<ActionResult> {
  return await lifecycle(
    guildId, giveawayId, "giveaway.extend", "extend",
    extendSchema.parse, raw, "Time added to the giveaway.",
  );
}

export async function shortenGiveawayAction(
  guildId: string,
  giveawayId: string,
  raw: unknown,
): Promise<ActionResult> {
  return await lifecycle(
    guildId, giveawayId, "giveaway.shorten", "shorten",
    shortenSchema.parse, raw, "Time removed from the giveaway.",
  );
}

export async function endGiveawayAction(
  guildId: string,
  giveawayId: string,
  raw: unknown,
): Promise<ActionResult> {
  return await lifecycle(
    guildId, giveawayId, "giveaway.end", "end",
    endSchema.parse, raw, "Giveaway ended. Winners are being drawn.",
  );
}

export async function cancelGiveawayAction(
  guildId: string,
  giveawayId: string,
  raw: unknown,
): Promise<ActionResult> {
  return await lifecycle(
    guildId, giveawayId, "giveaway.cancel", "cancel",
    cancelSchema.parse, raw, "Giveaway cancelled. No winner will be selected.",
  );
}

export async function rerollGiveawayAction(
  guildId: string,
  giveawayId: string,
  raw: unknown,
): Promise<ActionResult> {
  return await lifecycle(
    guildId, giveawayId, "giveaway.reroll", "reroll",
    rerollSchema.parse, raw, "Reroll requested with fresh, published randomness.",
  );
}

export async function revealGiveawayAction(
  guildId: string,
  giveawayId: string,
): Promise<ActionResult> {
  try {
    const user = await guard("reveal", guildId);
    const commandId = await enqueue(guildId, giveawayId, "giveaway.reveal", {}, user.userId, user.username);
    revalidatePath(`/admin/guilds/${guildId}/giveaways/${giveawayId}`);
    return { ok: true, commandId, message: "Seed reveal posted to the Discord channel." };
  } catch (error) {
    return fail(error);
  }
}

/** Disqualify or restore a participant. */
export async function moderateEntryAction(
  guildId: string,
  giveawayId: string,
  userId: string,
  eligible: boolean,
  reason: string,
): Promise<ActionResult> {
  try {
    const ctx = await guard("moderate", guildId);
    const data = entrySchema.parse({ user_id: userId, reason });
    const kind = eligible ? "giveaway.entry.restore" : "giveaway.entry.disqualify";
    const commandId = await enqueue(
      guildId, giveawayId,
      eligible ? "entry.restore" : "entry.disqualify",
      data as never,
      ctx.userId, ctx.username,
    );
    void kind;
    revalidatePath(`/admin/guilds/${guildId}/giveaways/${giveawayId}`);
    return {
      ok: true,
      commandId,
      message: eligible ? "Entry restored." : "Entry disqualified.",
    };
  } catch (error) {
    return fail(error);
  }
}

/** Set, change or disable the message-activity requirement. */
export async function setMessageRequirementAction(
  guildId: string,
  giveawayId: string,
  raw: unknown,
): Promise<ActionResult> {
  try {
    const user = await guard("message_requirement", guildId);
    const data = messageRequirementSchema.parse(raw);

    if (await hasPendingCommand(giveawayId)) {
      return {
        ok: false,
        message: "A previous command for this giveaway is still being processed.",
      };
    }

    const commandId = await enqueue(
      guildId, giveawayId, "giveaway.message_requirement", data as never, user.userId, user.username,
    );
    revalidatePath(`/admin/guilds/${guildId}/giveaways/${giveawayId}`);
    return {
      ok: true,
      commandId,
      message:
        data.min_messages === 0
          ? "Message requirement disabled."
          : `Message requirement set to ${data.min_messages} messages.`,
    };
  } catch (error) {
    return fail(error);
  }
}

/** Re-check every participant against the current message requirement. */
export async function revalidateActivityAction(
  guildId: string,
  giveawayId: string,
): Promise<ActionResult> {
  try {
    const user = await guard("revalidate_activity", guildId);
    revalidateSchema.parse({});
    const commandId = await enqueue(
      guildId, giveawayId, "giveaway.activity_revalidate", {}, user.userId, user.username,
    );
    revalidatePath(`/admin/guilds/${guildId}/giveaways/${giveawayId}`);
    return {
      ok: true,
      commandId,
      message: "Re-checking participants against the message requirement.",
    };
  } catch (error) {
    return fail(error);
  }
}

/** Read the current command state, for the status strip. */
export async function getCommandStatusAction(commandId: number): Promise<{
  status: string;
  error: string | null;
  result: unknown;
}> {
  const row = await first<{ status: string; last_error: string | null; result_json: string | null }>(
    "SELECT status, last_error, result_json FROM command_queue WHERE id = ?",
    [commandId],
  );
  let result: unknown = null;
  if (row?.result_json) {
    try {
      result = JSON.parse(row.result_json);
    } catch {
      result = null;
    }
  }
  return { status: row?.status ?? "unknown", error: row?.last_error ?? null, result };
}