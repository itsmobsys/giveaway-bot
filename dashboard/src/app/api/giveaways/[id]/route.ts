/**
 * Public JSON for a single giveaway: rules, counts, winners and winner history.
 *
 * Deliberately excludes everything private - no account timestamps, no
 * per-user message counts, no role or channel ID lists (only counts), no
 * participant identities.
 */

import { getPublicGiveaway, listDraws, listWinners } from "@/lib/giveaways";

export const dynamic = "force-dynamic";

export async function GET(
  _request: Request,
  { params }: { params: Promise<{ id: string }> },
): Promise<Response> {
  const { id } = await params;
  const giveaway = await getPublicGiveaway(id);
  if (!giveaway) {
    return Response.json({ error: "Giveaway not found" }, { status: 404 });
  }

  const [{ latest, history }, draws] = await Promise.all([
    listWinners(id),
    listDraws(id),
  ]);

  return Response.json(
    {
      giveaway,
      winners: latest,
      history,
      draws: draws.map((draw) => ({
        id: draw.id,
        round: draw.round,
        participant_count: draw.participant_count,
        winner_count: draw.winner_count,
        seed_commitment: draw.seed_commitment,
        participant_digest: draw.participant_digest,
        trigger_reason: draw.trigger_reason,
        created_at: draw.created_at,
      })),
    },
    {
      headers: {
        // Short cache: the bot changes this data, and stale counts look broken.
        "Cache-Control": "public, max-age=15, stale-while-revalidate=45",
      },
    },
  );
}