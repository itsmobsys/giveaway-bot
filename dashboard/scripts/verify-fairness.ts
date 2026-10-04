/**
 * Cross-language verification.
 *
 * Proves the TypeScript implementation in `src/lib/fairness.ts` produces
 * byte-identical results to the Python implementation in
 * `bot/giveaway_bot/fairness.py`, using the shared vectors.
 *
 *   node scripts/verify-fairness.mjs
 */

import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import {
  commitment,
  participantDigest,
  scoreEntry,
  verifyDraw,
  METHOD,
} from "../src/lib/fairness.ts";
import type { Manifest } from "../src/lib/fairness.ts";

const here = dirname(fileURLToPath(import.meta.url));

function findVectors() {
  const candidates = [
    resolve(here, "../../shared/test_vectors.json"),
    resolve(here, "../shared/test_vectors.json"),
    resolve(process.cwd(), "shared/test_vectors.json"),
    resolve(process.cwd(), "../shared/test_vectors.json"),
  ];
  for (const candidate of candidates) {
    try {
      readFileSync(candidate, "utf8");
      return candidate;
    } catch {
      continue;
    }
  }
  return null;
}

let failures = 0;
function check(name: string, ok: boolean, detail = ""): void {
  if (ok) {
    console.log(`  [ok] ${name}`);
  } else {
    failures += 1;
    console.log(`  [FAIL] ${name}${detail ? `\n      ${detail}` : ""}`);
  }
}

console.log("Fairness cross-verification (TypeScript)");
console.log("=".repeat(60));

// --- 1. Shared vectors -------------------------------------------------------
const vectorPath = findVectors();
if (!vectorPath) {
  console.log("\nNo shared/test_vectors.json found.");
  console.log("Generate it with:  python -m giveaway_bot genvectors");
  process.exit(2);
}

const payload = JSON.parse(readFileSync(vectorPath, "utf8")) as {
  algorithm: string;
  vectors: Array<{
    giveaway_id: string;
    user_id: string;
    entry_seq: number;
    total_entries: number;
    seed: string;
    commitment: string;
    score: string;
    retry_count: number;
  }>;
  vectors_participant_digest: string;
};

console.log(`\nVectors (${vectorPath})`);
check("algorithm id matches the spec", payload.algorithm === METHOD, `got ${payload.algorithm}`);

for (const vector of payload.vectors) {
  const { score, retries } = scoreEntry(
    vector.giveaway_id,
    vector.user_id,
    vector.entry_seq,
    vector.seed,
    vector.total_entries,
  );
  check(
    `score ${vector.giveaway_id}:${vector.user_id}#${vector.entry_seq}`,
    score === vector.score && retries === vector.retry_count,
    `ts=${score}/${retries} py=${vector.score}/${vector.retry_count}`,
  );
  check(
    `commitment ${vector.seed.slice(0, 12)}...`,
    commitment(vector.seed) === vector.commitment,
  );
}

const digest = participantDigest(
  payload.vectors.map((v) => ({ user_id: v.user_id, entry_seq: v.entry_seq })),
);
check(
  "participant digest",
  digest === payload.vectors_participant_digest,
  `ts=${digest} py=${payload.vectors_participant_digest}`,
);

// --- 2. Tamper detection -----------------------------------------------------
console.log("\nTamper detection");

const sample = payload.vectors[0];
if (!sample) {
  console.log("  [FAIL] no vectors available for tamper tests");
  failures += 1;
} else {
  const manifest: Manifest = {
    algorithm: METHOD,
    algorithm_version: "v1",
    giveaway_id: sample.giveaway_id,
    round: 1,
    seed: sample.seed,
    seed_commitment: sample.commitment,
    participant_digest: digest,
    participant_count: payload.vectors.length,
    winner_count: 1,
    winner_count_requested: 1,
    shortfall: 0,
    winners: [],
    scores: payload.vectors.map((v) => ({
      user_id: v.user_id,
      entry_seq: v.entry_seq,
      score: v.score,
      retry_count: v.retry_count,
    })),
  };

  // NOTE: each vector in the shared file was generated with its own seed, so they
  // are independent single-entry cases rather than one draw. To exercise the
  // verifier end to end we re-score them all against `sample.seed` with the real
  // derivation - exactly what a draw does.
  //
  // The giveaway_id differs per vector (they come from several giveaways), so a
  // single synthetic draw groups them by giveaway to stay faithful to the rule
  // that all entries in one draw share a giveaway_id.
  const groups = new Map<string, typeof payload.vectors>();
  for (const v of payload.vectors) {
    const bucket = groups.get(v.giveaway_id) ?? [];
    bucket.push(v);
    groups.set(v.giveaway_id, bucket);
  }

  const derivedScores: Array<{ user_id: string; entry_seq: number; score: string; retry_count: number }> = [];
  for (const [giveawayId, entries] of groups) {
    for (const v of entries) {
      const { score, retries } = scoreEntry(
        giveawayId,
        v.user_id,
        v.entry_seq,
        sample.seed,
        entries.length,
      );
      derivedScores.push({ user_id: v.user_id, entry_seq: v.entry_seq, score, retry_count: retries });
    }
  }
  const total = derivedScores.length;

  // Use a giveaway_id that matches every entry so they form one draw.
  const syntheticGiveawayId = "gw_fairness_verify";
  const rekeyedScores = derivedScores.map((entry) => {
    const { score, retries } = scoreEntry(
      syntheticGiveawayId,
      entry.user_id,
      entry.entry_seq,
      sample.seed,
      total,
    );
    return { user_id: entry.user_id, entry_seq: entry.entry_seq, score, retry_count: retries };
  });

  const derived: Manifest = {
    algorithm: METHOD,
    algorithm_version: "v1",
    giveaway_id: syntheticGiveawayId,
    round: 1,
    seed: sample.seed,
    seed_commitment: sample.commitment,
    participant_digest: participantDigest(rekeyedScores),
    participant_count: total,
    winner_count: 1,
    winner_count_requested: 1,
    shortfall: 0,
    winners: [],
    scores: rekeyedScores,
  };

  // Recompute the true winner ordering for a valid baseline.
  const ranked = derived.scores
    .map((s) => ({ ...s, big: BigInt(s.score) }))
    .sort((a, b) => (a.big !== b.big ? (a.big < b.big ? -1 : 1) : a.user_id < b.user_id ? -1 : 1));
  derived.winners = [
    {
      rank: 1,
      user_id: ranked[0]?.user_id ?? "",
      entry_seq: ranked[0]?.entry_seq ?? 1,
      score: ranked[0]?.score ?? "0",
    },
  ];

  const verify = (m: Manifest, seed = derived.seed) =>
    verifyDraw({
      giveawayId: derived.giveaway_id,
      seed,
      expectedCommitment: derived.seed_commitment,
      expectedDigest: derived.participant_digest,
      manifest: m,
    });

  const clean = verify(derived);
  check("a correctly derived manifest verifies", clean.ok, clean.errors.join("; "));
  check(
    "the derived winner is the lowest score",
    clean.winners[0]?.user_id === ranked[0]?.user_id,
    `verifier=${clean.winners[0]?.user_id} expected=${ranked[0]?.user_id}`,
  );

  check("a different seed fails verification", !verify(derived, "0".repeat(64)).ok);

  const editedScore = structuredClone(derived);
  editedScore.scores[0] = { ...editedScore.scores[0]!, score: "1" };
  check("an edited score is detected", !verify(editedScore).ok);

  // Swapping the winner must be caught even though the scores stay valid.
  const swappedWinner = structuredClone(derived);
  const last = swappedWinner.scores[swappedWinner.scores.length - 1];
  swappedWinner.winners = [
    {
      rank: 1,
      user_id: last?.user_id ?? "",
      entry_seq: last?.entry_seq ?? 1,
      score: last?.score ?? "0",
    },
  ];
  check("a swapped winner is detected", !verify(swappedWinner).ok);

  // Dropping a participant changes n, which changes every score - and the
  // participant digest catches it before the scores even matter.
  const dropped = structuredClone(derived);
  dropped.scores = dropped.scores.slice(0, -1);
  dropped.participant_count = dropped.scores.length;
  check("a dropped participant is detected", !verify(dropped).ok);

  // Injecting a fabricated entrant must also fail.
  const injected = structuredClone(derived);
  injected.scores.push({
    user_id: "999999999999999999",
    entry_seq: 1,
    score: "0",
    retry_count: 0,
  });
  check("an injected participant is detected", !verify(injected).ok);
}

console.log("\n" + "=".repeat(60));
if (failures > 0) {
  console.log(`FAILED: ${failures} check(s) failed`);
  process.exit(1);
}
console.log("OK: TypeScript implementation matches the Python implementation.");


