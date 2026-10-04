import Link from "next/link";
import { notFound, redirect } from "next/navigation";

import { Countdown, Timestamp } from "@/components/countdown";
import {
  AuditLog,
  GiveawayControls,
  MessageRequirementEditor,
  ParticipantTable,
} from "@/components/admin/giveaway-controls";
import { CopyableCode } from "@/components/copyable-code";
import { Card, EmptyState, Stat, StatusBadge } from "@/components/ui";
import { requireGuildAdmin } from "@/lib/auth";
import {
  getPublicGiveaway,
  listAudit,
  listDraws,
  listParticipants,
  listWinners,
} from "@/lib/giveaways";
import { verifyDraw } from "@/lib/fairness";
import { first } from "@/lib/db";
import { readSession } from "@/lib/session";

export const dynamic = "force-dynamic";

export const metadata = {
  robots: { index: false, follow: false },
};

interface Props {
  params: Promise<{ guildId: string; giveawayId: string }>;
  searchParams: Promise<{ q?: string }>;
}

export default async function GiveawayAdminPage({ params, searchParams }: Props) {
  const { guildId, giveawayId } = await params;
  const { q } = await searchParams;

  const user = await readSession();
  if (!user) redirect(`/login?redirect_to=${encodeURIComponent(`/admin/guilds/${guildId}/giveaways/${giveawayId}`)}`);

  // Authorize against Discord before touching any giveaway data.
  const auth = await requireGuildAdmin(guildId);
  if (!auth.allowed) {
    return (
      <Card>
        <EmptyState
          icon="🔒"
          title="Not authorised"
          description={
            auth.reason === "not_a_member_or_bot_lacks_access"
              ? "You are not a member of this server, or the bot cannot see members here. Make sure the bot is in the server and that you have Manage Server permission."
              : "You need Manage Server (or Administrator) permission in this server to view its giveaways."
          }
          action={
            <Link
              href="/admin"
              className="rounded-lg border border-[var(--border)] px-4 py-2 text-sm font-medium hover:bg-[var(--surface-muted)]"
            >
              Back to your servers
            </Link>
          }
        />
      </Card>
    );
  }

  const giveaway = await getPublicGiveaway(giveawayId);
  if (!giveaway || giveaway.guild_id !== guildId) notFound();

  const [{ rows, total }, winners, draws, audit] = await Promise.all([
    listParticipants(giveawayId, { limit: 25, search: q ?? "" }),
    listWinners(giveawayId),
    listDraws(giveawayId),
    listAudit(giveawayId, 30),
  ]);

  // Independently re-verify the newest draw in the browser-facing code path.
  const latestDraw = draws[0] ?? null;
  let verification: ReturnType<typeof verifyDraw> | null = null;
  if (latestDraw) {
    const record = await first<{ manifest_json: string }>(
      "SELECT manifest_json FROM giveaway_draws WHERE id = ?",
      [latestDraw.id],
    );
    if (record) {
      verification = verifyDraw({
        giveawayId,
        seed: latestDraw.seed,
        expectedCommitment: latestDraw.seed_commitment,
        expectedDigest: latestDraw.participant_digest,
        manifest: record.manifest_json,
      });
    }
  }

  return (
    <div className="space-y-6">
      <nav className="text-sm text-[var(--muted-foreground)]">
        <Link href="/admin" className="hover:underline">Admin</Link>
        {" / "}
        <Link href={`/admin/guilds/${guildId}`} className="hover:underline">
          {giveaway.guild_name ?? "Server"}
        </Link>
        {" / Giveaway"}
      </nav>

      <header className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <div className="flex items-center gap-2">
            <StatusBadge status={giveaway.status} />
            <a
              href={`/giveaways/${giveaway.id}`}
              className="text-xs text-[var(--accent)] hover:underline"
            >
              View public page →
            </a>
          </div>
          <h1 className="mt-2 text-2xl font-semibold tracking-tight">{giveaway.title}</h1>
          <p className="tabular mt-1 font-mono text-xs text-[var(--muted-foreground)]">
            {giveaway.id}
          </p>
        </div>

        <div className="text-right">
          <p className="text-xs text-[var(--muted-foreground)]">
            {giveaway.status === "ended" ? "Ended" : "Ends"}
          </p>
          <p className="tabular text-lg font-semibold">
            <Countdown endsAt={giveaway.ends_at} status={giveaway.status} />
          </p>
        </div>
      </header>

      <Card title="Controls" description="Every action is sent to the bot and recorded in the audit log.">
        <GiveawayControls
          guildId={guildId}
          giveawayId={giveawayId}
          status={giveaway.status}
          title={giveaway.title}
        />
      </Card>

      <div className="grid gap-4 sm:grid-cols-4">
        <Stat label="Participants" value={giveaway.participant_count} />
        <Stat label="Entries" value={giveaway.entry_count} />
        <Stat label="Winners" value={giveaway.winner_count} />
        <Stat label="Draws" value={giveaway.total_draws} />
      </div>

      <Card
        title="Message activity requirement"
        description="Require entrants to have posted a minimum number of messages before they can enter."
      >
        <MessageRequirementEditor
          guildId={guildId}
          giveawayId={giveawayId}
          current={giveaway.min_messages}
          scope={giveaway.message_count_scope}
          channelCount={giveaway.message_count_channel_count}
        />
      </Card>

      <Card
        id="participants"
        title={`Participants (${total})`}
        description={
          giveaway.min_messages > 0
            ? `Message counts are shown next to each entry. The requirement is ${giveaway.min_messages} message(s).`
            : "Manage entries here. Counts refresh after each action."
        }
      >
        <ParticipantTable
          guildId={guildId}
          giveawayId={giveawayId}
          participants={rows}
          total={total}
          requiredMessages={giveaway.min_messages}
        />
      </Card>

      {giveaway.status === "ended" && (
        <div className="grid gap-6 lg:grid-cols-2">
          <Card title="Winners">
            {winners.latest.length === 0 ? (
              <EmptyState
                icon="🏁"
                title="No winners"
                description="This giveaway ended without a draw, or nobody was eligible."
              />
            ) : (
              <ol className="space-y-2">
                {winners.latest.map((winner) => (
                  <li
                    key={winner.user_id}
                    className="flex items-center gap-3 rounded-lg border border-[var(--border)] bg-[var(--surface-muted)] p-3"
                  >
                    <span className="tabular grid size-8 place-items-center rounded-full bg-[var(--accent)] text-sm font-bold text-[var(--accent-foreground)]">
                      {winner.rank}
                    </span>
                    <div>
                      <a
                        href={`https://discord.com/users/${winner.user_id}`}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="font-mono text-sm hover:text-[var(--accent)] hover:underline"
                      >
                        {winner.user_id}
                      </a>
                      <p className="tabular text-xs text-[var(--muted-foreground)]">
                        score {winner.score}
                      </p>
                    </div>
                  </li>
                ))}
              </ol>
            )}
          </Card>

          <Card
            title="🔐 Draw verification"
            description="Recomputed in this deployment, independently of the bot."
          >
            {!latestDraw ? (
              <p className="text-sm text-[var(--muted-foreground)]">
                No draw has run yet.
              </p>
            ) : (
              <div className="space-y-3 text-sm">
                <div
                  className={`rounded-lg px-3 py-2 text-sm font-medium ${
                    verification?.ok
                      ? "bg-[var(--success)]/12 text-[var(--success)]"
                      : "bg-[var(--danger)]/12 text-[var(--danger)]"
                  }`}
                >
                  {verification?.ok
                    ? `Verified: all ${verification.checks.length} checks passed`
                    : `Verification failed: ${verification?.errors.join("; ")}`}
                </div>

                <CopyableCode label="Round" value={String(latestDraw.round)} truncate={6} />
                <CopyableCode label="Seed" value={latestDraw.seed} truncate={20} />
                <CopyableCode
                  label="Commitment"
                  value={latestDraw.seed_commitment}
                  truncate={20}
                />
                <CopyableCode
                  label="Participants"
                  value={latestDraw.participant_digest}
                  truncate={20}
                />

                {verification && !verification.ok && (
                  <ul className="list-inside list-disc text-xs text-[var(--danger)]">
                    {verification.errors.slice(0, 8).map((error) => (
                      <li key={error}>{error}</li>
                    ))}
                  </ul>
                )}
              </div>
            )}
          </Card>
        </div>
      )}

      {draws.length > 1 && (
        <Card title="Draw history" description="Every round, including rerolls.">
          <div className="overflow-x-auto scrollbar-thin">
            <table className="w-full text-left text-sm">
              <thead>
                <tr className="border-b border-[var(--border)] text-xs uppercase tracking-wide text-[var(--muted-foreground)]">
                  <th scope="col" className="py-2 pr-4">Round</th>
                  <th scope="col" className="py-2 pr-4">Entries</th>
                  <th scope="col" className="py-2 pr-4">Winners</th>
                  <th scope="col" className="py-2 pr-4">Trigger</th>
                  <th scope="col" className="py-2">When</th>
                </tr>
              </thead>
              <tbody>
                {draws.map((draw) => (
                  <tr key={draw.id} className="border-b border-[var(--border)] last:border-0">
                    <td className="tabular py-2.5 pr-4 font-medium">{draw.round}</td>
                    <td className="tabular py-2.5 pr-4">{draw.participant_count}</td>
                    <td className="tabular py-2.5 pr-4">{draw.winner_count}</td>
                    <td className="py-2.5 pr-4 text-[var(--muted-foreground)]">
                      {draw.trigger_reason.replace(/_/g, " ")}
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

      <Card title="Audit log" description="Append-only. Nothing here can be edited or deleted.">
        <AuditLog entries={audit} />
      </Card>
    </div>
  );
}
