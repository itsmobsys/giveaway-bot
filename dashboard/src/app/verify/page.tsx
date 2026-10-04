import Link from "next/link";

export const metadata = {
  title: "How to verify a draw",
};

const code = `import hashlib, hmac

def score(giveaway_id, user_id, entry_seq, seed_hex, n):
    key = bytes.fromhex(seed_hex)
    msg = f"{giveaway_id}:{user_id}:{entry_seq}".encode()
    limit = (2**256 // n) * n          # unbiased reduction bound
    attempt = 0
    r = int.from_bytes(hmac.new(key, msg, hashlib.sha256).digest(), "big")
    while r >= limit:                  # rejection sampling
        attempt += 1
        r = int.from_bytes(
            hmac.new(key, msg + f":{attempt}".encode(), hashlib.sha256).digest(), "big"
        )
    return r // n                      # score

# 1. the commitment must match the seed
assert hashlib.sha256(bytes.fromhex(seed)).hexdigest() == seed_commitment

# 2. rebuild the frozen entry list and check its digest
lines = sorted(f"{u}:{s}" for u, s in entries)
assert hashlib.sha256("\\n".join(lines).encode()).hexdigest() == participant_digest

# 3. score every entry against the same n
scored = [(score(giveaway_id, u, s, seed, len(entries)), u, s) for u, s in entries]

# 4. lowest scores win; ties break on (user_id, entry_seq)
scored.sort(key=lambda t: (t[0], t[1], t[2]))
winners = scored[:winner_count]`;

export default function VerifyPage() {
  return (
    <div className="mx-auto max-w-3xl space-y-6">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight">How to verify a draw</h1>
        <p className="mt-1 text-sm text-[var(--muted-foreground)]">
          A giveaway bot that tells you it is fair is asking you to trust it. This page exists so
          you do not have to. Everything below is enough to check any draw from scratch.
        </p>
      </header>

      <section className="rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--surface)] p-5">
        <h2 className="font-semibold">Why this is fair</h2>
        <ul className="mt-3 space-y-2 text-sm">
          <li>
            <strong>The seed is committed before anyone can enter.</strong> When a giveaway opens,
            the bot publishes <code className="font-mono text-xs">sha256(seed)</code> in the
            Discord message and on the giveaway page. Swapping in a different seed afterwards would
            produce a different hash, so the cheat is detectable by anyone reading the message
            history.
          </li>
          <li>
            <strong>The seed comes from the operating system CSPRNG.</strong> Not{" "}
            <code className="font-mono text-xs">Math.random()</code>, not a seeded PRNG, not
            anything the operator can influence.
          </li>
          <li>
            <strong>Every entrant is scored identically.</strong> The score is an HMAC of the
            entry, reduced modulo the number of entries with rejection sampling, so there is no
            modulo bias and no way to favour a particular account.
          </li>
          <li>
            <strong>Ranking has no hidden inputs.</strong> The winner is the lowest score. There is
            no weight, no priority, no owner override, and no code path anywhere that accepts a
            winner ID.
          </li>
        </ul>
      </section>

      <section className="rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--surface)] p-5">
        <h2 className="font-semibold">Check it yourself</h2>
        <p className="mt-1 text-sm text-[var(--muted-foreground)]">
          Fetch a draw&apos;s data, then run this. It is the entire algorithm - there is nothing
          else to it.
        </p>
        <pre className="scrollbar-thin mt-3 overflow-x-auto rounded-lg bg-[var(--surface-muted)] p-4 text-xs leading-relaxed">
          <code>{code}</code>
        </pre>
        <p className="mt-3 text-sm text-[var(--muted-foreground)]">
          Get the data for any draw from{" "}
          <code className="font-mono text-xs">/api/giveaways/&lt;id&gt;/verify</code>, which returns
          the seed, the commitment, the participant digest and every entry&apos;s score.
        </p>
      </section>

      <section className="rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--surface)] p-5">
        <h2 className="font-semibold">What an operator <em>can</em> do</h2>
        <p className="mt-1 text-sm text-[var(--muted-foreground)]">
          Being honest about the limits matters more than the guarantees:
        </p>
        <ul className="mt-3 list-inside list-disc space-y-2 text-sm">
          <li>
            An operator can end or cancel a giveaway, or refuse to run one. Those are visible
            refusals in the audit log and the public history, not bias.
          </li>
          <li>
            An operator can disqualify a participant (for example for an alt account). This is
            recorded with a reason, is flagged rather than silently deleted, and appears in the
            published manifest.
          </li>
          <li>
            An operator can reroll. Each reroll publishes a <em>new</em> seed and commitment, and
            every round stays on record - so grinding rerolls until a favourite wins is visible.
          </li>
          <li>
            An operator with direct database access could forge a draw entirely. The published
            pre-entry commitment is what makes that detectable: it was sent to Discord before any
            entries existed, and it will not match a fabricated seed.
          </li>
        </ul>
      </section>

      <p className="text-sm">
        <Link href="/giveaways" className="text-[var(--accent)] hover:underline">
          ← Back to giveaways
        </Link>
      </p>
    </div>
  );
}