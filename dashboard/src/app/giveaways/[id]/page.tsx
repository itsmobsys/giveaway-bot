import { notFound } from "next/navigation";
import type { Metadata } from "next";

import { Countdown, Timestamp } from "@/components/countdown";
import { CopyableCode } from "@/components/copyable-code";
import { Card, EmptyState, Stat, StatusBadge } from "@/components/ui";
import {
  getPublicGiveaway,
  listDraws,
  listWinners,
} from "@/lib/giveaways";

export const dynamic = "force-dynamic";

interface Props {
  params: Promise<{ id: string }>;
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const { id } = await params;
  const giveaway = await getPublicGiveaway(id);
  if (!giveaway) return { title: "Giveaway not found" };
  return {
    title: giveaway.title,
    description: giveaway.description || `Win ${giveaway.prize || "a prize"}`,
  };
}

export default async function GiveawayPage({ params }: Props) {
  const { id } = await params;
  const giveaway = await getPublicGiveaway(id);
  if (!giveaway) notFound();

  const [{ latest, history }, draws] = await Promise.all([
    listWinners(giveaway.id),
    listDraws(giveaway.id),
  ]);

  const rules: string[] = [
    giveaway.winner_count > 1
      ? `${giveaway.winner_count} winners will be drawn`
      : "1 winner will be drawn",
    giveaway.max_entries_per_user > 1
      ? `Up to ${giveaway.max_entries_per_user} entries per person`
      : "1 entry per person",
  ];
  if (giveaway.entry_limit > 0) rules.push(`Entry cap: ${giveaway.entry_limit} total`);
  if (giveaway.required_role_count > 0) {
    rules.push(
      giveaway.required_role_count === 1
        ? "1 specific role required"
        : `Requires ${giveaway.required_mode === "all" ? "all" : "one of"} ${giveaway.required_role_count} specific roles`,
    );
  }
  if (giveaway.blacklist_role_count > 0) {
    rules.push(`${giveaway.blacklist_role_count} role(s) excluded`);
  }
  if (giveaway.channel_restricted) rules.push("Specific channels only");
  if (giveaway.min_account_age_days > 0) {
    rules.push(`Account must be at least ${giveaway.min_account_age_days} day(s) old`);
  }
  if (giveaway.min_guild_join_days > 0) {
    rules.push(`Must have joined this server ${giveaway.min_guild_join_days} day(s) ago`);
  }
  if (giveaway.min_messages > 0) {
    rules.push(
      `Requires ${giveaway.min_messages} messages in ${
        giveaway.message_count_scope === "channel"
          ? `${giveaway.message_count_channel_count} specific channel(s)`
          : "the server"
      }`,
    );
  }

  const latestDraw = draws[0];

  return (
    <div className="space-y-6">
      <header className="space-y-3">
        <div className="flex flex-wrap items-center gap-2">
          <StatusBadge status={giveaway.status} />
          {giveaway.guild_name && (
            <span className="text-sm text-[var(--muted-foreground)]">{giveaway.guild_name}</span>
          )}
        </div>
        <h1 className="text-3xl font-bold tracking-tight">{giveaway.title}</h1>
        {giveaway.description && (
          <p className="max-w-3xl whitespace-pre-wrap text-[var(--muted-foreground)]">
            {giveaway.description}
          </p>
        )}
      </header>

      {giveaway.status !== "ended" && (
        <Card className="animate-scale-in border-[var(--accent)]/40 bg-[var(--accent)]/[0.06]">
          <div className="flex flex-wrap items-center justify-between gap-4">
            <div>
              <p className="text-xs uppercase tracking-wide text-[var(--muted-foreground)]">
                {giveaway.status === "paused" ? "Paused with" : "Time remaining"}
              </p>
              <p className="tabular mt-1 text-2xl font-semibold">
                <Countdown endsAt={giveaway.ends_at} status={giveaway.status} />
              </p>
            </div>
            <a
              href={`https://discord.com/channels/${giveaway.guild_id}/${giveaway.channel_id}`}
              target="_blank"
              rel="noopener noreferrer"
              className="rounded-lg bg-[var(--accent)] px-4 py-2 text-sm font-medium text-[var(--accent-foreground)] transition-transform hover:scale-[1.02]"
            >
              Enter on Discord
            </a>
          </div>
        </Card>
      )}

      <div className="grid gap-4 sm:grid-cols-4">
        <Stat label="Participants" value={giveaway.participant_count} />
        <Stat label="Total entries" value={giveaway.entry_count} />
        <Stat label="Winners" value={giveaway.winner_count} />
        <Stat
          label={giveaway.status === "ended" ? "Ended" : "Ends"}
          value={<Timestamp value={giveaway.ends_at} />}
        />
      </div>

      <div className="grid gap-6 lg:grid-cols-[2fr_1fr]">
        <div className="space-y-6">
          {giveaway.status === "ended" && latest.length > 0 && (
            <Card title="🏆 Winners">
              <ol className="space-y-3">
                {latest.map((winner) => (
                  <li
                    key={`${winner.round}-${winner.rank}-${winner.user_id}`}
                    className="flex items-center gap-3 rounded-lg border border-[var(--border)] bg-[var(--surface-muted)] p-3"
                  >
                    <span className="tabular grid size-8 shrink-0 place-items-center rounded-full bg-[var(--accent)] text-sm font-bold text-[var(--accent-foreground)]">
                      {winner.rank}
                    </span>
                    <div className="min-w-0">
                      {/* Discord ID only - no scraped usernames, nothing private. */}
                      <a
                        href={`https://discord.com/users/${winner.user_id}`}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="font-mono text-sm hover:text-[var(--accent)] hover:underline"
                      >
                        {winner.user_id}
                      </a>
                      <p className="text-xs text-[var(--muted-foreground)]">
                        score {winner.score} · round {winner.round}
                      </p>
                    </div>
                  </li>
                ))}
              </ol>
            </Card>
          )}

          {giveaway.status === "ended" && latest.length === 0 && (
            <Card>
              <EmptyState
                icon="🏁"
                title="No winners"
                description="This giveaway ended without any eligible participants entering, so nobody was selected."
              />
            </Card>
          )}

          {history.length > 0 && (
            <Card
              title="Winner history"
              description="Every draw round, including rerolls. Each one keeps its own seed and proof."
            >
              <div className="overflow-x-auto scrollbar-thin">
                <table className="w-full text-left text-sm">
                  <thead>
                    <tr className="border-b border-[var(--border)] text-xs uppercase tracking-wide text-[var(--muted-foreground)]">
                      <th scope="col" className="py-2 pr-4">Round</th>
                      <th scope="col" className="py-2 pr-4">Entries</th>
                      <th scope="col" className="py-2 pr-4">Winners</th>
                      <th scope="col" className="py-2 pr-4">Seed commitment</th>
                      <th scope="col" className="py-2">When</th>
                    </tr>
                  </thead>
                  <tbody>
                    {draws.map((draw) => (
                      <tr key={draw.id} className="border-b border-[var(--border)] last:border-0">
                        <td className="tabular py-2.5 pr-4 font-medium">{draw.round}</td>
                        <td className="tabular py-2.5 pr-4">{draw.participant_count}</td>
                        <td className="tabular py-2.5 pr-4">{draw.winner_count}</td>
                        <td className="py-2.5 pr-4">
                          <code
                            className="rounded bg-[var(--surface-muted)] px-1.5 py-0.5 font-mono text-xs"
                            title={draw.seed_commitment}
                          >
                            {draw.seed_commitment.slice(0, 16)}…
                          </code>
                        </td>
                        <td className="py-2.5 text-[var(--muted-foreground)]">
                          <Timestamp value={draw.created_at} />
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </Card>
          )}

          <Card title="Entry rules" description="Published in full so every entrant knows the rules up front.">
            <ul className="space-y-1.5 text-sm">
              {rules.map((rule) => (
                <li key={rule} className="flex gap-2">
                  <span aria-hidden="true" className="text-[var(--accent)]">•</span>
                  {rule}
                </li>
              ))}
            </ul>
          </Card>
        </div>

        <aside className="space-y-6">
          {giveaway.seed_commitment && (
            <Card title="🔐 Provably fair draw">
              <div className="space-y-4 text-sm">
                <div>
                  <p className="text-xs uppercase tracking-wide text-[var(--muted-foreground)]">
                    Seed commitment
                  </p>
                  <p className="mt-1 text-xs text-[var(--muted-foreground)]">
                    Published the moment entries opened. Anyone can check that the revealed seed
                    matches it.
                  </p>
                  <div className="mt-2">
                    <CopyableCode value={giveaway.seed_commitment} truncate={20} />
                  </div>
                </div>

                {latestDraw && giveaway.status === "ended" ? (
                  <div>
                    <p className="text-xs uppercase tracking-wide text-[var(--muted-foreground)]">
                      Revealed seed (round {latestDraw.round})
                    </p>
                    <div className="mt-2">
                      <CopyableCode value={latestDraw.seed} truncate={20} />
                    </div>
                    <p className="mt-2 text-xs text-[var(--muted-foreground)]">
                      Participant digest{" "}
                      <code className="font-mono">{latestDraw.participant_digest.slice(0, 16)}…</code>
                    </p>
                  </div>
                ) : (
                  <p className="text-xs text-[var(--muted-foreground)]">
                    The seed is still sealed. It will be revealed here as soon as the draw runs.
                  </p>
                )}

                <a
                  href="/verify"
                  className="inline-block text-sm text-[var(--accent)] hover:underline"
                >
                  How to verify this yourself →
                </a>
              </div>
            </Card>
          )}

          <Card title="About this giveaway">
            <dl className="space-y-2 text-sm">
              <div className="flex justify-between gap-2">
                <dt className="text-[var(--muted-foreground)]">Created</dt>
                <dd><Timestamp value={giveaway.created_at} /></dd>
              </div>
              <div className="flex justify-between gap-2">
                <dt className="text-[var(--muted-foreground)]">Prize count</dt>
                <dd className="tabular">{giveaway.prize_count}</dd>
              </div>
              <div className="flex justify-between gap-2">
                <dt className="text-[var(--muted-foreground)]">Draws</dt>
                <dd className="tabular">{giveaway.total_draws}</dd>
              </div>
              {giveaway.ended_reason && (
                <div className="flex justify-between gap-2">
                  <dt className="text-[var(--muted-foreground)]">Ended because</dt>
                  <dd className="text-right">{giveaway.ended_reason.replace(/_/g, " ")}</dd>
                </div>
              )}
            </dl>
          </Card>
        </aside>
      </div>
    </div>
  );
}
