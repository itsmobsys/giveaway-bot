import Link from "next/link";

import { Countdown } from "@/components/countdown";
import { EmptyState, StatusBadge } from "@/components/ui";
import { listPublicGiveaways } from "@/lib/giveaways";

export const dynamic = "force-dynamic";

export default async function BrowsePage() {
  const giveaways = await listPublicGiveaways(30);

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight">Live giveaways</h1>
        <p className="mt-1 text-sm text-[var(--muted-foreground)]">
          Winners are drawn with a provably fair commit–reveal algorithm. Every draw can be
          recomputed by anyone.
        </p>
      </header>

      {giveaways.length === 0 ? (
        <EmptyState
          icon="🎁"
          title="No giveaways yet"
          description="Once a giveaway is running in a Discord server it will appear here with its rules, entries and winner proof."
        />
      ) : (
        <ul className="grid gap-4 sm:grid-cols-2">
          {giveaways.map((giveaway) => (
            <li key={giveaway.id}>
              <Link
                href={`/giveaways/${giveaway.id}`}
                className="group flex h-full flex-col gap-3 rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--surface)] p-5 transition-all hover:-translate-y-0.5 hover:border-[var(--accent)] hover:shadow-lg"
              >
                <div className="flex items-start justify-between gap-3">
                  <h2 className="font-semibold leading-snug group-hover:text-[var(--accent)]">
                    {giveaway.title}
                  </h2>
                  <StatusBadge status={giveaway.status} />
                </div>

                {giveaway.guild_name && (
                  <p className="text-xs text-[var(--muted-foreground)]">
                    {giveaway.guild_name}
                  </p>
                )}

                {giveaway.prize && (
                  <p className="line-clamp-2 text-sm">
                    <span className="text-[var(--muted-foreground)]">Prize: </span>
                    {giveaway.prize}
                  </p>
                )}

                <dl className="tabular mt-auto grid grid-cols-3 gap-2 border-t border-[var(--border)] pt-3 text-center text-xs">
                  <div>
                    <dt className="text-[var(--muted-foreground)]">Entries</dt>
                    <dd className="text-sm font-semibold">{giveaway.participant_count}</dd>
                  </div>
                  <div>
                    <dt className="text-[var(--muted-foreground)]">Winners</dt>
                    <dd className="text-sm font-semibold">{giveaway.winner_count}</dd>
                  </div>
                  <div>
                    <dt className="text-[var(--muted-foreground)]">
                      {giveaway.status === "ended" ? "Ended" : "Ends in"}
                    </dt>
                    <dd className="text-sm font-semibold">
                      <Countdown endsAt={giveaway.ends_at} status={giveaway.status} />
                    </dd>
                  </div>
                </dl>
              </Link>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}