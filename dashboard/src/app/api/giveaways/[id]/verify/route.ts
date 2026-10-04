/**
 * Public draw manifest + independent verification.
 *
 * This is the endpoint a third party would use to audit a draw. It returns the
 * revealed seed, the commitment, the participant digest and every entry's
 * computed score - everything needed to recompute the winner locally.
 *
 * It does NOT compute the verdict itself beyond a convenience `verification`
 * block, because a verifier hosted by the same project as the draw is not
 * independent. The published algorithm and the vectors are what make the check
 * meaningful.
 */

import { first } from "@/lib/db";
import { getPublicGiveaway } from "@/lib/giveaways";
import { verifyDraw, type Manifest } from "@/lib/fairness";

export const dynamic = "force-dynamic";

export async function GET(
  request: Request,
  { params }: { params: Promise<{ id: string }> },
): Promise<Response> {
  const { id } = await params;
  // Route handlers only receive `params`, so read the query string directly.
  const draw = new URL(request.url).searchParams.get("draw") ?? undefined;

  const giveaway = await getPublicGiveaway(id);
  if (!giveaway) {
    return Response.json({ error: "Giveaway not found" }, { status: 404 });
  }

  // Pick the requested draw, or the newest one.
  const record = draw
    ? await first<{
        id: string;
        giveaway_id: string;
        round: number;
        method: string;
        algorithm_version: string;
        server_seed: string;
        seed_commitment: string;
        participant_digest: string;
        participant_count: number;
        winner_count: number;
        manifest_json: string;
        trigger_reason: string;
        created_at: number;
      }>("SELECT * FROM giveaway_draws WHERE id = ? AND giveaway_id = ?", [draw, id])
    : await first<{
        id: string;
        giveaway_id: string;
        round: number;
        method: string;
        algorithm_version: string;
        server_seed: string;
        seed_commitment: string;
        participant_digest: string;
        participant_count: number;
        winner_count: number;
        manifest_json: string;
        trigger_reason: string;
        created_at: number;
      }>("SELECT * FROM giveaway_draws WHERE giveaway_id = ? ORDER BY round DESC LIMIT 1", [id]);

  if (!record) {
    return Response.json(
      {
        giveaway_id: id,
        status: giveaway.status,
        drawn: false,
        message: "No draw has run yet. The seed is still sealed.",
        seed_commitment: giveaway.seed_commitment,
      },
      { status: 200 },
    );
  }

  let manifest: Manifest;
  try {
    manifest = JSON.parse(record.manifest_json) as Manifest;
  } catch {
    return Response.json({ error: "Stored manifest is unreadable" }, { status: 500 });
  }

  const verification = verifyDraw({
    giveawayId: record.giveaway_id,
    seed: record.server_seed,
    expectedCommitment: record.seed_commitment,
    expectedDigest: record.participant_digest,
    manifest,
  });

  return Response.json(
    {
      draw_id: record.id,
      giveaway_id: record.giveaway_id,
      round: record.round,
      algorithm: record.method,
      algorithm_version: record.algorithm_version,
      trigger_reason: record.trigger_reason,
      created_at: record.created_at,
      // Commitment published when entries opened, for comparison with the seed.
      seed_commitment_at_open: giveaway.seed_commitment,
      seed: record.server_seed,
      seed_commitment: record.seed_commitment,
      participant_digest: record.participant_digest,
      participant_count: record.participant_count,
      winner_count: record.winner_count,
      manifest,
      verification,
      how_to_verify: [
        "1. Check seed_commitment === sha256(seed).",
        "2. Rebuild the entry list from manifest.scores (sorted by user_id, entry_seq) and check participant_digest === sha256(lines.join('\\n')).",
        "3. For each entry compute HMAC-SHA256(key=seed, msg=`${giveaway_id}:${user_id}:${entry_seq}`) as a big-endian integer.",
        "4. Reject and retry with ':1', ':2', ... appended while value >= floor(2^256 / n) * n.",
        "5. score = value // n. Sort ascending by (score, user_id, entry_seq); the first winner_count entries win.",
        "Full spec: shared/FAIRNESS_SPEC.md",
      ],
    },
    {
      headers: {
        "Cache-Control": "public, max-age=60, stale-while-revalidate=300",
      },
    },
  );
}