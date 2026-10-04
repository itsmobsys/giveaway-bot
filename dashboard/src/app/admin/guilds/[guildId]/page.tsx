import Link from "next/link";
import { redirect } from "next/navigation";

import { Countdown } from "@/components/countdown";
import { Card, EmptyState, Stat, StatusBadge } from "@/components/ui";
import { requireGuildAdmin } from "@/lib/auth";
import { getAnalytics, listGuildGiveaways } from "@/lib/giveaways";
import { first } from "@/lib/db";
import { readSession } from "@/lib/session";

import { CreateGiveawayForm } from "./create-form";

export const dynamic = "force-dynamic";

export const metadata = {
  robots: { index: false, follow: false },
};

interface Props {
  params: Promise<{ guildId: string }>;
}

export default async function GuildAdminPage({ params }: Props) {
  const { guildId } = await params;
  const user = await readSession();
  if (!user) redirect(`/login?redirect_to=${encodeURIComponent(`/admin/guilds/${guildId}`)}`);

  const auth = await requireGuildAdmin(guildId);
  if (!auth.allowed) {
    return (
      <Card>
        <EmptyState
          icon="🔒"
          title="Not authorised"
          description="You need Manage Server (or Administrator) permission in this server."
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

  const [giveaways, analytics, guild] = await Promise.all([
    listGuildGiveaways(guildId, 50),
    getAnalytics(guildId),
    first<{ id: string; name: string; member_count: number }>(
      "SELECT id, name, member_count FROM guilds WHERE id = ?",
      [guildId],
    ),
  ]);

  const active = giveaways.find((g) =>
    ["scheduled", "running", "paused"].includes(g.status),
  );

  return (
    <div className="space-y-6">
      <nav className="text-sm text-[var(--muted-foreground)]">
        <Link href="/admin" className="hover:underline">Admin</Link>
        {" / "}
        <span className="text-[var(--foreground)]">{guild?.name ?? "Server"}</span>
      </nav>

      <header>
        <h1 className="text-2xl font-semibold tracking-tight">{guild?.name ?? "Server"}</h1>
        <p className="tabular mt-0.5 text-sm text-[var(--muted-foreground)]">
          {(guild?.member_count ?? 0).toLocaleString()} members · {guildId}
        </p>
      </header>

      <div className="grid gap-4 sm:grid-cols-4">
        <Stat label="Running" value={analytics.giveaways.running} />
        <Stat label="Ended" value={analytics.giveaways.ended} />
        <Stat label="Total entries" value={analytics.participation.entries} />
        <Stat label="Winners" value={analytics.participation.winners} />
      </div>

      {active ? (
        <Card
          title="Current giveaway"
          description="This server runs one giveaway at a time. End it before starting another."
          action={<StatusBadge status={active.status} />}
        >
          <div className="flex flex-wrap items-center justify-between gap-4">
            <div>
              <p className="font-medium">{active.title}</p>
              <p className="tabular mt-1 text-sm text-[var(--muted-foreground)]">
                {active.participant_count} participants · {active.entry_count} entries ·{" "}
                <Countdown endsAt={active.ends_at} status={active.status} />
              </p>
              {active.participant_role_id && (
                <p className="mt-1 text-xs text-[var(--muted-foreground)]">
                  Entrants role:{" "}
                  <code className="font-mono">{active.participant_role_id}</code> — ping it to
                  reach everyone who entered.
                </p>
              )}
            </div>
            <Link
              href={`/admin/guilds/${guildId}/giveaways/${active.id}`}
              className="rounded-lg bg-[var(--accent)] px-4 py-2 text-sm font-medium text-[var(--accent-foreground)]"
            >
              Manage
            </Link>
          </div>
        </Card>
      ) : (
        <CreateGiveawayForm guildId={guildId} />
      )}

      <Card title="All giveaways" description={`${giveaways.length} in this server`}>
        {giveaways.length === 0 ? (
          <EmptyState
            icon="🎁"
            title="No giveaways yet"
            description="Create the first giveaway above. It will appear in Discord within a few seconds."
          />
        ) : (
          <div className="overflow-x-auto scrollbar-thin">
            <table className="w-full text-left text-sm">
              <thead>
                <tr className="border-b border-[var(--border)] text-xs uppercase tracking-wide text-[var(--muted-foreground)]">
                  <th scope="col" className="py-2 pr-4">Title</th>
                  <th scope="col" className="py-2 pr-4">Status</th>
                  <th scope="col" className="py-2 pr-4">Entries</th>
                  <th scope="col" className="py-2 pr-4">Winners</th>
                  <th scope="col" className="py-2 pr-4">Ends</th>
                  <th scope="col" className="py-2" />
                </tr>
              </thead>
              <tbody>
                {giveaways.map((giveaway) => (
                  <tr key={giveaway.id} className="border-b border-[var(--border)] last:border-0">
                    <td className="py-2.5 pr-4 font-medium">{giveaway.title}</td>
                    <td className="py-2.5 pr-4"><StatusBadge status={giveaway.status} /></td>
                    <td className="tabular py-2.5 pr-4">{giveaway.entry_count}</td>
                    <td className="tabular py-2.5 pr-4">{giveaway.winner_count}</td>
                    <td className="py-2.5 pr-4 text-[var(--muted-foreground)]">
                      <Countdown endsAt={giveaway.ends_at} status={giveaway.status} />
                    </td>
                    <td className="py-2.5">
                      <Link
                        href={`/admin/guilds/${guildId}/giveaways/${giveaway.id}`}
                        className="text-xs text-[var(--accent)] hover:underline"
                      >
                        Manage →
                      </Link>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <Card
        title="Message activity"
        description="How much activity tracking is active in this server."
      >
        <dl className="grid grid-cols-2 gap-4 sm:grid-cols-4">
          <div>
            <dt className="text-xs text-[var(--muted-foreground)]">Tracked users</dt>
            <dd className="tabular text-lg font-semibold">{analytics.activity.tracked_users}</dd>
          </div>
          <div>
            <dt className="text-xs text-[var(--muted-foreground)]">Exact</dt>
            <dd className="tabular text-lg font-semibold">{analytics.activity.exact_users}</dd>
          </div>
          <div>
            <dt className="text-xs text-[var(--muted-foreground)]">Backfilled</dt>
            <dd className="tabular text-lg font-semibold">{analytics.activity.backfilled_users}</dd>
          </div>
          <div>
            <dt className="text-xs text-[var(--muted-foreground)]">Estimated</dt>
            <dd className="tabular text-lg font-semibold">{analytics.activity.estimated_users}</dd>
          </div>
        </dl>
      </Card>
    </div>
  );
}