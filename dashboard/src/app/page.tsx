import Link from "next/link";

import { listPublicGiveaways } from "@/lib/giveaways";

export const dynamic = "force-dynamic";

export default async function HomePage() {
  const running = await listPublicGiveaways(3, "running");

  return (
    <div className="space-y-12">
      <section className="animate-fade-in pt-6 text-center">
        <p className="text-sm font-medium text-[var(--accent)]">Open source · auditable</p>
        <h1 className="mx-auto mt-3 max-w-2xl text-balance text-4xl font-bold tracking-tight sm:text-5xl">
          Giveaways that nobody can rig
        </h1>
        <p className="mx-auto mt-4 max-w-xl text-balance text-[var(--muted-foreground)]">
          Winners are chosen with a commit–reveal draw. The seed commitment is published before
          anyone can enter, and after the draw everyone can recompute every score themselves.
        </p>
        <div className="mt-7 flex flex-wrap items-center justify-center gap-3">
          <Link
            href="/giveaways"
            className="rounded-lg bg-[var(--accent)] px-5 py-2.5 font-medium text-[var(--accent-foreground)] transition-transform hover:scale-[1.02]"
          >
            Browse giveaways
          </Link>
          <Link
            href="/admin"
            className="rounded-lg border border-[var(--border)] px-5 py-2.5 font-medium transition-colors hover:bg-[var(--surface-muted)]"
          >
            Admin panel
          </Link>
        </div>
      </section>

      <section className="grid gap-4 sm:grid-cols-3">
        {[
          {
            title: "Committed before entries",
            body: "A SHA-256 commitment to the random seed is published the moment a giveaway opens, so no operator can swap in a friendlier value afterwards.",
          },
          {
            title: "Cryptographically secure",
            body: "Scores come from HMAC-SHA256 over each entry, reduced with rejection sampling so every entrant has an identical chance.",
          },
          {
            title: "Reproducible by anyone",
            body: "After the draw the seed is revealed along with every participant's score. Recompute the winner yourself in about twenty lines of code.",
          },
        ].map((item) => (
          <div
            key={item.title}
            className="rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--surface)] p-5"
          >
            <h2 className="font-semibold">{item.title}</h2>
            <p className="mt-1.5 text-sm text-[var(--muted-foreground)]">{item.body}</p>
          </div>
        ))}
      </section>

      {running.length > 0 && (
        <section>
          <div className="mb-4 flex items-baseline justify-between">
            <h2 className="text-lg font-semibold">Running now</h2>
            <Link
              href="/giveaways"
              className="text-sm text-[var(--accent)] hover:underline"
            >
              See all →
            </Link>
          </div>
          <ul className="grid gap-3 sm:grid-cols-3">
            {running.map((giveaway) => (
              <li key={giveaway.id}>
                <Link
                  href={`/giveaways/${giveaway.id}`}
                  className="block rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--surface)] p-4 transition-colors hover:border-[var(--accent)]"
                >
                  <p className="truncate font-medium">{giveaway.title}</p>
                  <p className="tabular mt-1 text-xs text-[var(--muted-foreground)]">
                    {giveaway.participant_count} entries · {giveaway.winner_count} winner
                    {giveaway.winner_count === 1 ? "" : "s"}
                  </p>
                </Link>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}