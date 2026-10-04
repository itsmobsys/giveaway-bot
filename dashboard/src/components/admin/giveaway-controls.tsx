"use client";

import { useEffect, useState, useTransition } from "react";
import { useRouter } from "next/navigation";

import {
  cancelGiveawayAction,
  endGiveawayAction,
  extendGiveawayAction,
  moderateEntryAction,
  pauseGiveawayAction,
  rerollGiveawayAction,
  resumeGiveawayAction,
  revalidateActivityAction,
  setMessageRequirementAction,
  shortenGiveawayAction,
  type ActionResult,
} from "@/app/admin/actions";

import type { AuditRow, ParticipantRow } from "@/lib/giveaways";

/** Toast stack. Auto-dismisses, and errors stay longer. */
type Toast = { id: number; ok: boolean; message: string };

function useToasts() {
  const [toasts, setToasts] = useState<Toast[]>([]);

  function push(ok: boolean, message: string) {
    const id = Date.now() + Math.random();
    setToasts((current) => [...current, { id, ok, message }]);
    setTimeout(() => {
      setToasts((current) => current.filter((t) => t.id !== id));
    }, ok ? 4000 : 7000);
  }

  return { toasts, push };
}

function ToastList({ toasts }: { toasts: Toast[] }) {
  return (
    <div
      aria-live="polite"
      aria-atomic="false"
      className="pointer-events-none fixed bottom-4 right-4 z-50 flex w-full max-w-sm flex-col gap-2"
    >
      {toasts.map((toast) => (
        <div
          key={toast.id}
          role={toast.ok ? "status" : "alert"}
          className={`animate-scale-in pointer-events-auto rounded-lg border px-4 py-3 text-sm shadow-lg ${
            toast.ok
              ? "border-[var(--success)]/40 bg-[var(--success)]/12 text-[var(--success)]"
              : "border-[var(--danger)]/40 bg-[var(--danger)]/12 text-[var(--danger)]"
          }`}
        >
          {toast.message}
        </div>
      ))}
    </div>
  );
}

/** Confirmation dialog. Destructive actions require typing the giveaway name. */
function ConfirmButton({
  label,
  confirmLabel,
  requireText,
  variant = "secondary",
  disabled,
  onConfirm,
}: {
  label: string;
  confirmLabel: string;
  requireText?: string;
  variant?: "primary" | "secondary" | "danger";
  disabled?: boolean;
  onConfirm: () => Promise<ActionResult>;
}) {
  const [open, setOpen] = useState(false);
  const [typed, setTyped] = useState("");
  const [pending, startTransition] = useTransition();

  const confirmText = requireText ?? "";
  const canConfirm = typed.trim() === confirmText;

  const styles = {
    primary: "bg-[var(--accent)] text-[var(--accent-foreground)]",
    secondary: "border border-[var(--border)] hover:bg-[var(--surface-muted)]",
    danger: "bg-[var(--danger)] text-white",
  } as const;

  return (
    <>
      <button
        type="button"
        onClick={() => {
          setOpen(true);
          setTyped("");
        }}
        disabled={disabled || pending}
        className={`rounded-lg px-3.5 py-2 text-sm font-medium transition-colors disabled:opacity-50 ${styles[variant]}`}
      >
        {label}
      </button>

      {open && (
        <div
          className="fixed inset-0 z-50 grid place-items-center bg-black/50 p-4"
          role="dialog"
          aria-modal="true"
          aria-labelledby="confirm-title"
          onClick={(event) => {
            if (event.target === event.currentTarget) setOpen(false);
          }}
        >
          <div className="animate-scale-in w-full max-w-md rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--surface)] p-6 shadow-2xl">
            <h2 id="confirm-title" className="text-lg font-semibold">
              {confirmLabel}
            </h2>
            {requireText && (
              <>
                <p className="mt-2 text-sm text-[var(--muted-foreground)]">
                  Type <code className="rounded bg-[var(--surface-muted)] px-1.5 py-0.5 font-mono text-xs">
                    {requireText}
                  </code>{" "}
                  to confirm.
                </p>
                <input
                  type="text"
                  value={typed}
                  onChange={(event) => setTyped(event.target.value)}
                  autoFocus
                  aria-label={`Type ${requireText} to confirm`}
                  className="mt-3 w-full rounded-lg border border-[var(--border)] bg-[var(--background)] px-3 py-2 text-sm outline-none focus:border-[var(--accent)]"
                />
              </>
            )}
            <div className="mt-5 flex justify-end gap-2">
              <button
                type="button"
                onClick={() => setOpen(false)}
                className="rounded-lg border border-[var(--border)] px-3.5 py-2 text-sm font-medium hover:bg-[var(--surface-muted)]"
              >
                Cancel
              </button>
              <button
                type="button"
                disabled={!canConfirm || pending}
                onClick={() =>
                  startTransition(async () => {
                    const result = await onConfirm();
                    if (result.ok) setOpen(false);
                  })
                }
                className={`rounded-lg px-3.5 py-2 text-sm font-medium disabled:opacity-50 ${styles[variant]}`}
              >
                {pending ? "Working…" : confirmLabel}
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}

/** Live lifecycle controls. */
export function GiveawayControls({
  guildId,
  giveawayId,
  status,
  title,
}: {
  guildId: string;
  giveawayId: string;
  status: string;
  title: string;
}) {
  const { toasts, push } = useToasts();
  const [pending, startTransition] = useTransition();
  const [disabled, setDisabled] = useState(false);

  function run(label: string, fn: () => Promise<ActionResult>) {
    setDisabled(true);
    startTransition(async () => {
      const result = await fn();
      push(result.ok, result.message);
      setDisabled(false);
      // Re-fetch so counts and status reflect the bot's action.
      if (result.ok) window.location.reload();
    });
  }

  return (
    <>
      <ToastList toasts={toasts} />
      <div className="flex flex-wrap gap-2">
        {status === "running" && (
          <ActionButton
            label="Pause"
            disabled={disabled || pending}
            onClick={() =>
              run("pause", () => pauseGiveawayAction(guildId, giveawayId, {}))
            }
          />
        )}
        {status === "paused" && (
          <ActionButton
            label="Resume"
            primary
            disabled={disabled || pending}
            onClick={() =>
              run("resume", () => resumeGiveawayAction(guildId, giveawayId, {}))
            }
          />
        )}

        {(status === "running" || status === "paused") && (
          <>
            <DurationControl
              actionLabel="Extend"
              placeholder="e.g. 30m, 2h, 3d"
              disabled={disabled || pending}
              onSubmit={(duration) =>
                run("extend", () => extendGiveawayAction(guildId, giveawayId, { duration }))
              }
            />
            <DurationControl
              actionLabel="Shorten"
              placeholder="e.g. 30m"
              disabled={disabled || pending}
              onSubmit={(duration) =>
                run("shorten", () => shortenGiveawayAction(guildId, giveawayId, { duration }))
              }
            />
          </>
        )}

        {status !== "ended" && (
          <>
            <ConfirmButton
              label="End & draw"
              confirmLabel="End and draw winners"
              requireText={title}
              variant="danger"
              disabled={disabled || pending}
              onConfirm={() => endGiveawayAction(guildId, giveawayId, { revalidate_activity: true })}
            />
            <ConfirmButton
              label="Cancel"
              confirmLabel="Cancel without a winner"
              requireText={title}
              variant="danger"
              disabled={disabled || pending}
              onConfirm={() => cancelGiveawayAction(guildId, giveawayId, {})}
            />
          </>
        )}

        {status === "ended" && (
          <ConfirmButton
            label="Reroll"
            confirmLabel="Reroll with fresh randomness"
            variant="primary"
            disabled={disabled || pending}
            onConfirm={() => rerollGiveawayAction(guildId, giveawayId, { reason: "dashboard reroll" })}
          />
        )}
      </div>
    </>
  );
}

function ActionButton({
  label,
  primary,
  disabled,
  onClick,
}: {
  label: string;
  primary?: boolean;
  disabled?: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className={`rounded-lg px-3.5 py-2 text-sm font-medium transition-colors disabled:opacity-50 ${
        primary
          ? "bg-[var(--accent)] text-[var(--accent-foreground)]"
          : "border border-[var(--border)] hover:bg-[var(--surface-muted)]"
      }`}
    >
      {label}
    </button>
  );
}

function DurationControl({
  actionLabel,
  placeholder,
  disabled,
  onSubmit,
}: {
  actionLabel: string;
  placeholder: string;
  disabled: boolean;
  onSubmit: (duration: string) => void;
}) {
  const [value, setValue] = useState("");

  return (
    <form
      className="flex gap-1.5"
      onSubmit={(event) => {
        event.preventDefault();
        if (value.trim()) onSubmit(value.trim());
      }}
    >
      <input
        type="text"
        value={value}
        onChange={(event) => setValue(event.target.value)}
        placeholder={placeholder}
        aria-label={`${actionLabel} by a duration`}
        className="w-28 rounded-lg border border-[var(--border)] bg-[var(--background)] px-2.5 py-2 text-sm outline-none focus:border-[var(--accent)] disabled:opacity-50"
      />
      <button
        type="submit"
        disabled={disabled || !value.trim()}
        className="rounded-lg border border-[var(--border)] px-3.5 py-2 text-sm font-medium hover:bg-[var(--surface-muted)] disabled:opacity-50"
      >
        {actionLabel}
      </button>
    </form>
  );
}

/** Message-activity requirement editor. */
export function MessageRequirementEditor({
  guildId,
  giveawayId,
  current,
  scope,
  channelCount,
}: {
  guildId: string;
  giveawayId: string;
  current: number;
  scope: string;
  channelCount: number;
}) {
  const { toasts, push } = useToasts();
  const [value, setValue] = useState(String(current));
  const [pending, startTransition] = useTransition();

  return (
    <>
      <ToastList toasts={toasts} />
      <form
        className="flex flex-wrap items-end gap-2"
        onSubmit={(event) => {
          event.preventDefault();
          startTransition(async () => {
            const parsed = Number(value);
            if (!Number.isInteger(parsed) || parsed < 0 || parsed > 100000) {
              push(false, "Enter a whole number between 0 and 100000.");
              return;
            }
            const result = await setMessageRequirementAction(guildId, giveawayId, {
              min_messages: parsed,
              message_count_scope: parsed > 0 ? scope : "guild",
              message_count_channel_ids: [],
              message_count_ignore_bots: true,
            });
            push(result.ok, result.message);
            if (result.ok) window.location.reload();
          });
        }}
      >
        <div>
          <label
            htmlFor="min-messages"
            className="block text-xs font-medium text-[var(--muted-foreground)]"
          >
            Messages required (0 disables)
          </label>
          <input
            id="min-messages"
            type="number"
            min={0}
            max={100000}
            value={value}
            onChange={(event) => setValue(event.target.value)}
            className="tabular mt-1 w-32 rounded-lg border border-[var(--border)] bg-[var(--background)] px-2.5 py-2 text-sm outline-none focus:border-[var(--accent)]"
          />
        </div>
        <p className="flex-1 text-xs text-[var(--muted-foreground)]">
          Currently counting{" "}
          {scope === "channel"
            ? `${channelCount} specific channel(s)`
            : "the whole server"}
          .
        </p>
        <button
          type="submit"
          disabled={pending}
          className="rounded-lg bg-[var(--accent)] px-3.5 py-2 text-sm font-medium text-[var(--accent-foreground)] disabled:opacity-50"
        >
          {pending ? "Saving…" : "Save requirement"}
        </button>
        <button
          type="button"
          disabled={pending || current === 0}
          onClick={() =>
            startTransition(async () => {
              const result = await revalidateActivityAction(guildId, giveawayId);
              push(result.ok, result.message);
            })
          }
          className="rounded-lg border border-[var(--border)] px-3.5 py-2 text-sm font-medium hover:bg-[var(--surface-muted)] disabled:opacity-50"
        >
          Re-check participants
        </button>
      </form>
    </>
  );
}

/** Participant table with inline moderation. */
export function ParticipantTable({
  guildId,
  giveawayId,
  participants,
  total,
  requiredMessages,
}: {
  guildId: string;
  giveawayId: string;
  participants: ParticipantRow[];
  total: number;
  requiredMessages: number;
}) {
  const { toasts, push } = useToasts();
  const router = useRouter();
  const [search, setSearch] = useState("");
  const [pending, startTransition] = useTransition();

  return (
    <>
      <ToastList toasts={toasts} />

      <form
        className="mb-4 flex gap-2"
        onSubmit={(event) => {
          event.preventDefault();
          const params = new URLSearchParams({ q: search });
          router.push(`?${params.toString()}#participants`);
        }}
      >
        <input
          type="search"
          value={search}
          onChange={(event) => setSearch(event.target.value)}
          placeholder="Search by Discord ID"
          aria-label="Search participants"
          className="flex-1 rounded-lg border border-[var(--border)] bg-[var(--background)] px-3 py-2 text-sm outline-none focus:border-[var(--accent)]"
        />
        <button
          type="submit"
          className="rounded-lg border border-[var(--border)] px-3.5 py-2 text-sm font-medium hover:bg-[var(--surface-muted)]"
        >
          Search
        </button>
      </form>

      {participants.length === 0 ? (
        <p className="py-8 text-center text-sm text-[var(--muted-foreground)]">
          No participants yet.
        </p>
      ) : (
        <div className="overflow-x-auto scrollbar-thin">
          <table className="w-full text-left text-sm">
            <thead>
              <tr className="border-b border-[var(--border)] text-xs uppercase tracking-wide text-[var(--muted-foreground)]">
                <th scope="col" className="py-2 pr-4">Discord ID</th>
                <th scope="col" className="py-2 pr-4">Entries</th>
                {requiredMessages > 0 && (
                  <th scope="col" className="py-2 pr-4">Messages</th>
                )}
                <th scope="col" className="py-2 pr-4">Status</th>
                <th scope="col" className="py-2 pr-4">Joined</th>
                <th scope="col" className="py-2">Actions</th>
              </tr>
            </thead>
            <tbody>
              {participants.map((participant) => {
                const short = requiredMessages > 0
                  ? requiredMessages - (participant.message_count ?? 0)
                  : 0;
                return (
                  <tr key={participant.user_id} className="border-b border-[var(--border)] last:border-0">
                    <td className="py-2.5 pr-4 font-mono text-xs">{participant.user_id}</td>
                    <td className="tabular py-2.5 pr-4">{participant.entries}</td>
                    {requiredMessages > 0 && (
                      <td className="tabular py-2.5 pr-4">
                        <span className={short > 0 ? "text-[var(--warning)]" : "text-[var(--success)]"}>
                          {participant.message_count ?? 0}/{requiredMessages}
                        </span>
                      </td>
                    )}
                    <td className="py-2.5 pr-4">
                      <span
                        className={
                          participant.status === "valid"
                            ? "text-[var(--success)]"
                            : participant.status === "winner"
                              ? "text-[var(--accent)]"
                              : "text-[var(--danger)]"
                        }
                      >
                        {participant.status}
                      </span>
                      {participant.invalid_reason && (
                        <p className="text-xs text-[var(--muted-foreground)]">
                          {participant.invalid_reason}
                        </p>
                      )}
                    </td>
                    <td className="py-2.5 pr-4 text-xs text-[var(--muted-foreground)]">
                      {participant.last_joined_at
                        ? new Date(participant.last_joined_at).toLocaleDateString()
                        : "—"}
                    </td>
                    <td className="py-2.5">
                      {participant.status === "disqualified" ? (
                        <button
                          type="button"
                          disabled={pending}
                          onClick={() =>
                            startTransition(async () => {
                              const result = await moderateEntryAction(
                                guildId, giveawayId, participant.user_id, true, "restored from dashboard",
                              );
                              push(result.ok, result.message);
                              if (result.ok) router.refresh();
                            })
                          }
                          className="rounded border border-[var(--border)] px-2 py-1 text-xs hover:bg-[var(--surface-muted)]"
                        >
                          Restore
                        </button>
                      ) : (
                        <button
                          type="button"
                          disabled={pending}
                          onClick={() =>
                            startTransition(async () => {
                              const result = await moderateEntryAction(
                                guildId, giveawayId, participant.user_id, false, "flagged from dashboard",
                              );
                              push(result.ok, result.message);
                              if (result.ok) router.refresh();
                            })
                          }
                          className="rounded border border-[var(--danger)]/40 px-2 py-1 text-xs text-[var(--danger)] hover:bg-[var(--danger)]/10"
                        >
                          Disqualify
                        </button>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      <p className="mt-3 text-xs text-[var(--muted-foreground)]">
        Showing {participants.length} of {total} participants.
      </p>
    </>
  );
}

/** Audit log list. */
export function AuditLog({ entries }: { entries: AuditRow[] }) {
  if (entries.length === 0) {
    return <p className="text-sm text-[var(--muted-foreground)]">No activity recorded yet.</p>;
  }
  return (
    <ol className="space-y-2">
      {entries.map((entry) => (
        <li
          key={entry.id}
          className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5 rounded-lg border border-[var(--border)] bg-[var(--surface-muted)] px-3 py-2 text-xs"
        >
          <code className="font-mono">{entry.action}</code>
          <span className="text-[var(--muted-foreground)]">
            by {entry.actor_name ?? entry.actor_id ?? "system"}
          </span>
          <span className="text-[var(--muted-foreground)]">via {entry.source}</span>
          {entry.outcome !== "success" && (
            <span className="text-[var(--danger)]">({entry.outcome})</span>
          )}
          <span className="tabular ml-auto text-[var(--muted-foreground)]">
            {new Date(entry.created_at).toLocaleString()}
          </span>
        </li>
      ))}
    </ol>
  );
}