import { redirect } from "next/navigation";

import { listAdminGuilds } from "@/lib/auth";
import { readSession } from "@/lib/session";
import { getAnalytics } from "@/lib/giveaways";

import { Card, EmptyState, Stat } from "@/components/ui";
import { Timestamp } from "@/components/countdown";
import { SignOutButton } from "./sign-out-button";

export const dynamic = "force-dynamic";

export const metadata = {
  robots: { index: false, follow: false },
};

export default async function AdminHome() {
  const user = await readSession();
  if (!user) redirect("/login?redirect_to=/admin");

  const guilds = await listAdminGuilds(user.id);
  const withAnalytics = await Promise.all(
    guilds.map(async (guild) => ({ guild, analytics: await getAnalytics(guild.id) })),
  );

  const totals = withAnalytics.reduce(
    (acc, item) => ({
      giveaways: acc.giveaways + item.analytics.giveaways.total,
      entries: acc.entries + item.analytics.participation.entries,
      participants: acc.participants + item.analytics.participation.participants,
      winners: acc.winners + item.analytics.participation.winners,
    }),
    { giveaways: 0, entries: 0, participants: 0, winners: 0 },
  );

  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Admin</h1>
          <p className="mt-0.5 text-sm text-[var(--muted-foreground)]">
            Signed in as{" "}
            <span className="text-[var(--foreground)]">
              {user.globalName ?? user.username}
            </span>
          </p>
        </div>
        <SignOutButton />
      </header>

      <div className="grid gap-4 sm:grid-cols-4">
        <Stat label="Giveaways" value={totals.giveaways} />
        <Stat label="Entries" value={totals.entries} />
        <Stat label="Participants" value={totals.participants} />
        <Stat label="Winners" value={totals.winners} />
      </div>

      {guilds.length === 0 ? (
        <Card>
          <EmptyState
            icon="🏠"
            title="No servers found"
            description="You are not a server administrator anywhere the bot is present. Invite the bot to a server where you have Manage Server, or ask an existing admin to add you."
          />
        </Card>
      ) : (
        <div className="grid gap-4 md:grid-cols-2">
          {withAnalytics.map(({ guild, analytics }) => (
            <a
              key={guild.id}
              href={`/admin/guilds/${guild.id}`}
              className="group rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--surface)] p-5 transition-all hover:-translate-y-0.5 hover:border-[var(--accent)] hover:shadow-lg"
            >
              <div className="flex items-center gap-3">
                {guild.icon_url ? (
                  // eslint-disable-next-line @next/next/no-img-element
                  <img
                    src={guild.icon_url}
                    alt=""
                    className="size-10 rounded-lg"
                    width={40}
                    height={40}
                  />
                ) : (
                  <span className="grid size-10 place-items-center rounded-lg bg-[var(--surface-muted)] text-lg">
                    🏠
                  </span>
                )}
                <div className="min-w-0">
                  <h2 className="truncate font-semibold group-hover:text-[var(--accent)]">
                    {guild.name}
                  </h2>
                  <p className="tabular text-xs text-[var(--muted-foreground)]">
                    {guild.member_count.toLocaleString()} members
                  </p>
                </div>
              </div>

              <dl className="tabular mt-4 grid grid-cols-4 gap-2 border-t border-[var(--border)] pt-3 text-center text-xs">
                {(["total", "running", "paused", "ended"] as const).map((key) => (
                  <div key={key}>
                    <dt className="text-[var(--muted-foreground)] capitalize">{key}</dt>
                    <dd className="text-sm font-semibold">{analytics.giveaways[key]}</dd>
                  </div>
                ))}
              </dl>
            </a>
          ))}
        </div>
      )}

      <p className="text-xs text-[var(--muted-foreground)]">
        Data last refreshed <Timestamp value={Date.now()} />.
      </p>
    </div>
  );
}