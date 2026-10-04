/**
 * Fairness: an independent TypeScript implementation of the algorithm in
 * `shared/FAIRNESS_SPEC.md`.
 *
 * This file exists so a third party can verify a draw without trusting the bot.
 * It is deliberately a mirror of `bot/giveaway_bot/fairness.py` - not a wrapper
 * around it - and the two are cross-checked against `shared/test_vectors.json`.
 *
 * Differences that matter:
 *  - scores are exchanged as decimal strings, because `Number` cannot hold a
 *    256-bit integer and BigInt/decimal mismatch would silently change rankings
 *  - every comparison is done on BigInt, never on lexicographic strings
 */

import { createHmac, createHash } from "node:crypto";

export const ALGORITHM_VERSION = "v1";
export const ALGORITHM_ID = "hmac-sha256-commit-reveal";
export const METHOD = `${ALGORITHM_ID}/${ALGORITHM_VERSION}`;

const TWO_POW_256 = 1n << 256n;
const SEED_BYTES = 32;

export interface FrozenEntry {
  user_id: string;
  entry_seq: number;
  entry_id?: number | null;
}

export interface ManifestScore {
  user_id: string;
  entry_seq: number;
  score: string;
  retry_count: number;
}

/**
 * Draw winners for a frozen entry set (used by tooling and tests).
 *
 * Mirrors `bot/giveaway_bot/fairness.py::draw_winners` so a manifest generated
 * here is byte-identical to one the bot would produce.
 */
export function drawForSeed(input: {
  giveawayId: string;
  seed: string;
  entries: Array<{ user_id: string; entry_seq: number; entry_id?: number | null }>;
  winnerCount: number;
  round?: number;
}): {
  manifest: Manifest;
  winners: Array<{ rank: number; user_id: string; entry_seq: number; score: string }>;
} {
  const giveawayId = input.giveawayId;
  const entries = [...input.entries].sort((a, b) =>
    a.user_id === b.user_id ? a.entry_seq - b.entry_seq : a.user_id < b.user_id ? -1 : 1,
  );
  const n = entries.length;
  const seed = input.seed;

  const scored = entries.map((entry) => {
    const { score, retries } = scoreEntry(
      giveawayId,
      entry.user_id,
      entry.entry_seq,
      seed,
      n,
    );
    return { ...entry, score, retry_count: retries, big: BigInt(score) };
  });

  const ranked = [...scored].sort((a, b) => {
    if (a.big !== b.big) return a.big < b.big ? -1 : 1;
    if (a.user_id !== b.user_id) return a.user_id < b.user_id ? -1 : 1;
    return a.entry_seq - b.entry_seq;
  });

  const winners = ranked.slice(0, Math.min(input.winnerCount, n)).map((entry, index) => ({
    rank: index + 1,
    user_id: entry.user_id,
    entry_seq: entry.entry_seq,
    score: entry.score,
  }));

  const manifest: Manifest = {
    algorithm: METHOD,
    algorithm_version: ALGORITHM_VERSION,
    giveaway_id: giveawayId,
    round: input.round ?? 1,
    seed,
    seed_commitment: commitment(seed),
    participant_digest: participantDigest(entries),
    participant_count: n,
    winner_count: winners.length,
    winner_count_requested: input.winnerCount,
    shortfall: Math.max(0, input.winnerCount - n),
    winners,
    scores: scored.map((entry) => ({
      user_id: entry.user_id,
      entry_seq: entry.entry_seq,
      score: entry.score,
      retry_count: entry.retry_count,
    })),
  };

  return { manifest, winners };
}

export interface Manifest {
  algorithm: string;
  algorithm_version: string;
  giveaway_id: string;
  round: number;
  seed: string;
  seed_commitment: string;
  participant_digest: string;
  participant_count: number;
  winner_count: number;
  winner_count_requested: number;
  shortfall: number;
  winners: Array<{ rank: number; user_id: string; entry_seq: number; score: string }>;
  scores: ManifestScore[];
}

/** Validate and decode a hex seed. */
export function seedBytes(seedHex: string): Buffer {
  if (typeof seedHex !== "string" || seedHex.length !== SEED_BYTES * 2) {
    throw new Error("seed must be a 64 character hex string");
  }
  if (!/^[0-9a-f]{64}$/i.test(seedHex)) throw new Error("seed is not valid hex");
  return Buffer.from(seedHex, "hex");
}

/** sha256(server_seed), lowercase hex. Published before entries open. */
export function commitment(seedHex: string): string {
  return createHash("sha256").update(seedBytes(seedHex)).digest("hex");
}

/** The exact bytes that get HMAC'd. Normative - do not reformat. */
export function entryMessage(giveawayId: string, userId: string, entrySeq: number): Buffer {
  if (!giveawayId || giveawayId.includes(":")) {
    throw new Error("giveaway_id must be non-empty and contain no ':'");
  }
  if (!/^\d+$/.test(userId)) throw new Error("user_id must be a decimal string");
  if (!Number.isInteger(entrySeq) || entrySeq < 1) {
    throw new Error("entry_seq must be an integer >= 1");
  }
  // entry_seq uses a bare decimal with NO zero padding.
  return Buffer.from(`${giveawayId}:${userId}:${entrySeq}`, "utf8");
}

/**
 * Score one entry.
 *
 * The reduction is unbiased via rejection sampling against the largest multiple
 * of `n` that fits in 2^256. Returns the score as a decimal string.
 */
export function scoreEntry(
  giveawayId: string,
  userId: string,
  entrySeq: number,
  seedHex: string,
  totalEntries: number,
): { score: string; retries: number } {
  if (!Number.isInteger(totalEntries) || totalEntries < 1) {
    throw new Error("total_entries must be an integer >= 1");
  }

  const key = seedBytes(seedHex);
  const message = entryMessage(giveawayId, userId, entrySeq);
  const limit = (TWO_POW_256 / BigInt(totalEntries)) * BigInt(totalEntries);

  let digest = createHmac("sha256", key).update(message).digest();
  let value = BigInt(`0x${digest.toString("hex")}`);
  let retries = 0;

  while (value >= limit) {
    retries += 1;
    if (retries > 256) throw new Error("rejection sampling failed to converge");
    const retryMessage = Buffer.concat([message, Buffer.from(`:${retries}`, "utf8")]);
    digest = createHmac("sha256", key).update(retryMessage).digest();
    value = BigInt(`0x${digest.toString("hex")}`);
  }

  return { score: (value / BigInt(totalEntries)).toString(), retries };
}

/** SHA-256 over the frozen entry set, sorted for canonical ordering. */
export function participantDigest(entries: Array<{ user_id: string; entry_seq: number }>): string {
  const normalised = entries
    .map((e) => [String(e.user_id), Number(e.entry_seq)] as const)
    .sort((a, b) => (a[0] === b[0] ? a[1] - b[1] : a[0] < b[0] ? -1 : 1));

  const payload = normalised.map(([userId, seq]) => `${userId}:${seq}`).join("\n");
  return createHash("sha256").update(payload, "utf8").digest("hex");
}

/**
 * Independently recompute a stored manifest.
 *
 * This is the verifier: given only the seed and the manifest, it reproduces every
 * score and the winner ordering, and reports each check rather than a single
 * boolean so an auditor can see what actually failed.
 */
export function verifyDraw(input: {
  giveawayId: string;
  seed: string;
  expectedCommitment: string;
  expectedDigest: string;
  manifest: Manifest | string;
}): {
  ok: boolean;
  checks: Array<{ name: string; ok: boolean; expected?: string; actual?: string }>;
  errors: string[];
  winners: Array<{ rank: number; user_id: string; entry_seq: number; score: string }>;
  participantDigest: string;
} {
  const checks: Array<{ name: string; ok: boolean; expected?: string; actual?: string }> = [];
  const errors: string[] = [];

  const record = (name: string, ok: boolean, expected?: string, actual?: string): void => {
    checks.push({ name, ok, expected, actual });
    if (!ok) errors.push(`${name} mismatch`);
  };

  let manifest: Manifest;
  if (typeof input.manifest === "string") {
    try {
      manifest = JSON.parse(input.manifest) as Manifest;
    } catch {
      return {
        ok: false,
        checks: [],
        errors: ["manifest is not valid JSON"],
        winners: [],
        participantDigest: "",
      };
    }
  } else {
    manifest = input.manifest;
  }

  // 1. The commitment must bind this seed.
  let actualCommitment = "";
  try {
    actualCommitment = commitment(input.seed);
  } catch (error) {
    errors.push(`seed is invalid: ${error instanceof Error ? error.message : String(error)}`);
    return { ok: false, checks, errors, winners: [], participantDigest: "" };
  }
  record(
    "seed_commitment",
    actualCommitment === input.expectedCommitment.toLowerCase(),
    input.expectedCommitment.toLowerCase(),
    actualCommitment,
  );

  // 2. The participant digest must match the frozen entry list.
  const scores = manifest.scores ?? [];
  const digest = participantDigest(scores);
  record("participant_digest", digest === input.expectedDigest, input.expectedDigest, digest);

  // 3. participant_count must agree with the number of scores.
  if (manifest.participant_count !== scores.length) {
    record(
      "participant_count",
      false,
      String(manifest.participant_count),
      String(scores.length),
    );
  }

  // 4. Recompute every score against the same n.
  const total = scores.length;
  for (const item of scores) {
    try {
      const { score } = scoreEntry(
        input.giveawayId,
        String(item.user_id),
        Number(item.entry_seq),
        input.seed,
        total,
      );
      record(`score[${item.user_id}#${item.entry_seq}]`, score === String(item.score), String(item.score), score);
    } catch (error) {
      errors.push(error instanceof Error ? error.message : String(error));
    }
  }

  // 5. Recompute the winner ordering.
  const ranked = scores
    .map((item) => ({
      user_id: String(item.user_id),
      entry_seq: Number(item.entry_seq),
      score: BigInt(String(item.score)),
    }))
    .sort((a, b) => {
      if (a.score !== b.score) return a.score < b.score ? -1 : 1;
      if (a.user_id !== b.user_id) return a.user_id < b.user_id ? -1 : 1;
      return a.entry_seq - b.entry_seq;
    });

  const declaredWinners = (manifest.winners ?? []).map((w) => ({
    rank: w.rank,
    user_id: String(w.user_id),
    entry_seq: Number(w.entry_seq),
    score: String(w.score),
  }));

  const take = Math.min(ranked.length, declaredWinners.length);
  const recomputedWinners = ranked.slice(0, take).map((entry, index) => ({
    rank: index + 1,
    user_id: entry.user_id,
    entry_seq: entry.entry_seq,
    score: entry.score.toString(),
  }));

  const sameWinners =
    declaredWinners.length === recomputedWinners.length &&
    declaredWinners.every(
      (w, i) =>
        w.user_id === recomputedWinners[i]?.user_id &&
        w.entry_seq === recomputedWinners[i]?.entry_seq &&
        w.score === recomputedWinners[i]?.score,
    );
  record("winners", sameWinners);

  return {
    ok: errors.length === 0,
    checks,
    errors,
    winners: recomputedWinners,
    participantDigest: digest,
  };
}