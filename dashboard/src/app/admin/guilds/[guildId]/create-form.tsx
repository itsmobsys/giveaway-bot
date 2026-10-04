"use client";

import { useState, useTransition } from "react";

import { createGiveawayAction, type ActionResult } from "@/app/admin/actions";

/**
 * Create a giveaway.
 *
 * Fields marked required match the bot's own validation, but the bot re-validates
 * everything independently - this form is for operator convenience, not security.
 */
export function CreateGiveawayForm({ guildId }: { guildId: string }) {
  const [open, setOpen] = useState(false);
  const [pending, startTransition] = useTransition();
  const [result, setResult] = useState<ActionResult | null>(null);
  const [form, setForm] = useState({
    title: "",
    prize: "",
    description: "",
    duration: "24h",
    winner_count: 1,
    prize_count: 1,
    max_entries_per_user: 1,
    min_messages: 0,
    min_account_age_days: 0,
    required_role_ids: "",
    blacklist_role_ids: "",
  });

  const set = (key: keyof typeof form) => (value: string | number) =>
    setForm((current) => ({ ...current, [key]: value }));

  function submit(event: React.FormEvent) {
    event.preventDefault();
    setResult(null);

    // Never silently drop a typo'd role ID: the operator would think roles
    // were required when none were sent.
    const required = parseIds(form.required_role_ids);
    const blacklisted = parseIds(form.blacklist_role_ids);
    const dropped = [...required.dropped, ...blacklisted.dropped];
    if (dropped.length > 0) {
      setResult({
        ok: false,
        message: `Those don't look like role IDs and were not sent: ${dropped.slice(0, 5).join(", ")}${dropped.length > 5 ? ` (+${dropped.length - 5} more)` : ""}. Use plain IDs or <@&id> mentions.`,
      });
      return;
    }

    const payload = {
      ...form,
      winner_count: Number(form.winner_count),
      prize_count: Number(form.prize_count),
      max_entries_per_user: Number(form.max_entries_per_user),
      min_messages: Number(form.min_messages),
      min_account_age_days: Number(form.min_account_age_days),
      // Accept "123, 456" or "<@&123>" and send plain snowflakes.
      required_role_ids: required.ids,
      blacklist_role_ids: blacklisted.ids,
    };

    startTransition(async () => {
      const actionResult = await createGiveawayAction(guildId, payload);
      setResult(actionResult);
      if (actionResult.ok) {
        window.setTimeout(() => window.location.reload(), 1200);
      }
    });
  }

  return (
    <Card open={open} onToggle={() => setOpen((value) => !value)}>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="font-semibold">Create a giveaway</h2>
          <p className="text-sm text-[var(--muted-foreground)]">
            No giveaway is running right now.
          </p>
        </div>
        <button
          type="button"
          onClick={() => setOpen((value) => !value)}
          className="rounded-lg bg-[var(--accent)] px-4 py-2 text-sm font-medium text-[var(--accent-foreground)]"
        >
          {open ? "Close" : "New giveaway"}
        </button>
      </div>

      {result && (
        <p
          role="alert"
          className={`mt-4 rounded-lg px-3 py-2 text-sm ${
            result.ok
              ? "bg-[var(--success)]/12 text-[var(--success)]"
              : "bg-[var(--danger)]/12 text-[var(--danger)]"
          }`}
        >
          {result.message}
        </p>
      )}

      {open && (
        <form onSubmit={submit} className="mt-5 space-y-4">
          <Field label="Title" required>
            <input
              required
              maxLength={256}
              value={form.title}
              onChange={(e) => set("title")(e.target.value)}
              placeholder="Steam key giveaway"
              className={inputClass}
            />
          </Field>

          <Field label="Prize">
            <input
              maxLength={512}
              value={form.prize}
              onChange={(e) => set("prize")(e.target.value)}
              placeholder="3 game keys"
              className={inputClass}
            />
          </Field>

          <Field label="Description">
            <textarea
              rows={3}
              maxLength={4000}
              value={form.description}
              onChange={(e) => set("description")(e.target.value)}
              className={inputClass}
            />
          </Field>

          <div className="grid gap-4 sm:grid-cols-2">
            <Field label="Duration" hint="30m, 12h, 3d, or a plain number of minutes.">
              <input
                required
                value={form.duration}
                onChange={(e) => set("duration")(e.target.value)}
                placeholder="24h"
                className={inputClass}
              />
            </Field>

            <Field label="Channel" hint="Set by the bot, not per-giveaway.">
              <div className="rounded-lg border border-[var(--border)] bg-[var(--surface-muted)] px-3 py-2 text-sm text-[var(--muted-foreground)]">
                The bot&apos;s giveaway channel
              </div>
            </Field>
          </div>

          <div className="grid gap-4 sm:grid-cols-3">
            <Field label="Winners">
              <input
                type="number"
                min={1}
                max={20}
                value={form.winner_count}
                onChange={(e) => set("winner_count")(Number(e.target.value))}
                className={inputClass}
              />
            </Field>
            <Field label="Prize count">
              <input
                type="number"
                min={1}
                max={20}
                value={form.prize_count}
                onChange={(e) => set("prize_count")(Number(e.target.value))}
                className={inputClass}
              />
            </Field>
            <Field label="Entries per person">
              <input
                type="number"
                min={1}
                max={100}
                value={form.max_entries_per_user}
                onChange={(e) => set("max_entries_per_user")(Number(e.target.value))}
                className={inputClass}
              />
            </Field>
          </div>

          <details className="rounded-lg border border-[var(--border)] p-3">
            <summary className="cursor-pointer text-sm font-medium">
              Eligibility rules (optional)
            </summary>
            <div className="mt-3 space-y-4">
              <div className="grid gap-4 sm:grid-cols-2">
                <Field
                  label="Required role IDs"
                  hint="Comma separated. Entrants need one of these roles."
                >
                  <input
                    value={form.required_role_ids}
                    onChange={(e) => set("required_role_ids")(e.target.value)}
                    placeholder="111111111111111111, 222222222222222222"
                    className={`${inputClass} font-mono text-xs`}
                  />
                </Field>
                <Field label="Blacklisted role IDs" hint="Entrants with these roles cannot enter.">
                  <input
                    value={form.blacklist_role_ids}
                    onChange={(e) => set("blacklist_role_ids")(e.target.value)}
                    placeholder="333333333333333333"
                    className={`${inputClass} font-mono text-xs`}
                  />
                </Field>
              </div>

              <div className="grid gap-4 sm:grid-cols-2">
                <Field label="Minimum messages" hint="0 disables the requirement.">
                  <input
                    type="number"
                    min={0}
                    max={100000}
                    value={form.min_messages}
                    onChange={(e) => set("min_messages")(Number(e.target.value))}
                    className={inputClass}
                  />
                </Field>
                <Field label="Minimum account age (days)">
                  <input
                    type="number"
                    min={0}
                    max={3650}
                    value={form.min_account_age_days}
                    onChange={(e) => set("min_account_age_days")(Number(e.target.value))}
                    className={inputClass}
                  />
                </Field>
              </div>
            </div>
          </details>

          <div className="flex items-center gap-3">
            <button
              type="submit"
              disabled={pending}
              className="rounded-lg bg-[var(--accent)] px-5 py-2.5 text-sm font-medium text-[var(--accent-foreground)] disabled:opacity-50"
            >
              {pending ? "Creating…" : "Create giveaway"}
            </button>
            <p className="text-xs text-[var(--muted-foreground)]">
              Entrants will receive a temporary role so you can ping them, removed automatically
              when the giveaway ends.
            </p>
          </div>
        </form>
      )}
    </Card>
  );
}

const inputClass =
  "w-full rounded-lg border border-[var(--border)] bg-[var(--background)] px-3 py-2 text-sm outline-none focus:border-[var(--accent)]";

function Card({
  open,
  onToggle,
  children,
}: {
  open: boolean;
  onToggle: () => void;
  children: React.ReactNode;
}) {
  return (
    <section className="animate-fade-in rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--surface)] p-5">
      {children}
    </section>
  );
}

function Field({
  label,
  hint,
  required,
  children,
}: {
  label: string;
  hint?: string;
  required?: boolean;
  children: React.ReactNode;
}) {
  return (
    <label className="block">
      <span className="text-xs font-medium text-[var(--muted-foreground)]">
        {label}
        {required && <span className="text-[var(--danger)]"> *</span>}
      </span>
      <div className="mt-1">{children}</div>
      {hint && <span className="mt-1 block text-xs text-[var(--muted-foreground)]">{hint}</span>}
    </label>
  );
}

/** Accept raw IDs, comma/space separated lists, and Discord mention syntax. */
function parseIds(value: string): { ids: string[]; dropped: string[] } {
  const cleaned = value.replace(/<@&(\d+)>/g, "$1");
  const ids: string[] = [];
  const dropped: string[] = [];
  for (const item of new Set(
    cleaned
      .split(/[,\s]+/)
      .map((part) => part.trim())
      .filter((part) => part.length > 0),
  )) {
    if (/^[0-9]{15,25}$/.test(item)) ids.push(item);
    else dropped.push(item);
  }
  return { ids, dropped };
}