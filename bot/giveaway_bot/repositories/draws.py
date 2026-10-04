"""Draw and winner repositories.

``save_draw`` writes the draw record, the winners, the previous round's
``lost`` markers and the giveaway counters **in one caller-owned transaction**
so a draw can never be half-applied.
"""

from __future__ import annotations

import json
from typing import Any

from ..db import Database, now_ms
from ..fairness import ALGORITHM_VERSION, METHOD, DrawResult
from ..models import DrawRecord, WinnerRecord


def save_draw(
    db: Database,
    *,
    giveaway_id: str,
    result: DrawResult,
    trigger_reason: str,
    triggered_by: str | None,
    eligible_count: int,
    duration_ms: int | None,
) -> str:
    """Persist one draw round. Caller must already be inside a transaction."""
    draw_id = f"draw_{giveaway_id}_{result.round_number}_{now_ms()}"
    db.execute(
        """
        INSERT INTO giveaway_draws (
            id, giveaway_id, round, method, algorithm_version,
            server_seed, seed_commitment, participant_digest,
            participant_count, eligible_count, winner_count,
            manifest_json, triggered_by, trigger_reason, duration_ms, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            draw_id,
            giveaway_id,
            result.round_number,
            METHOD,
            ALGORITHM_VERSION,
            result.seed,
            result.commitment,
            result.participant_digest,
            result.participant_count,
            eligible_count,
            len(result.winners),
            result.to_json(),
            triggered_by,
            trigger_reason,
            duration_ms,
            now_ms(),
        ),
    )

    if result.winners:
        db.execute_many(
            """
            INSERT INTO giveaway_winners (
                giveaway_id, draw_id, round, user_id, rank, entry_id, entry_seq,
                score, server_seed, seed_commitment, awarded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    giveaway_id,
                    draw_id,
                    result.round_number,
                    winner.user_id,
                    winner.rank,
                    winner.entry_id,
                    winner.entry_seq,
                    winner.score,
                    result.seed,
                    result.commitment,
                    now_ms(),
                )
                for winner in result.winners
            ],
        )

    # Participation status is NOT mutated here. Entries keep `status='valid'` for
    # the life of the giveaway so that a reroll re-freezes the *same* set.
    # Round-specific outcomes live in giveaway_winners (one row per round),
    # which is what winner history reads.
    #
    # Marking rows 'winner'/'lost' would look tidy but would silently empty the
    # pool for every subsequent round - a correctness bug, not a style choice.

    db.execute(
        """
        UPDATE giveaways
        SET server_seed = ?, seed_commitment = ?, draw_round = ?, total_draws = total_draws + 1,
            seed_revealed_at = ?, updated_at = ?
        WHERE id = ?
        """,
        (result.seed, result.commitment, result.round_number, now_ms(), now_ms(), giveaway_id),
    )
    return draw_id


def latest_draw(db: Database, giveaway_id: str) -> DrawRecord | None:
    row = db.query_one(
        "SELECT * FROM giveaway_draws WHERE giveaway_id = ? ORDER BY round DESC LIMIT 1",
        (giveaway_id,),
    )
    return DrawRecord.from_row(row) if row else None


def list_draws(db: Database, giveaway_id: str) -> list[DrawRecord]:
    rows = db.query(
        "SELECT * FROM giveaway_draws WHERE giveaway_id = ? ORDER BY round DESC",
        (giveaway_id,),
    )
    return [DrawRecord.from_row(row) for row in rows]


def get_draw(db: Database, draw_id: str) -> DrawRecord | None:
    row = db.query_one("SELECT * FROM giveaway_draws WHERE id = ?", (draw_id,))
    return DrawRecord.from_row(row) if row else None


def list_winners(db: Database, giveaway_id: str, *, round_number: int | None = None) -> list[WinnerRecord]:
    sql = """
        SELECT w.user_id, w.rank, w.round, w.score, w.entry_seq, w.entry_id,
               w.draw_id, w.server_seed, w.seed_commitment, w.awarded_at
        FROM giveaway_winners w
        WHERE w.giveaway_id = ?
    """
    params: list[Any] = [giveaway_id]
    if round_number is not None:
        sql += " AND w.round = ?"
        params.append(round_number)
    sql += " ORDER BY w.round DESC, w.rank ASC"
    return [
        WinnerRecord(
            user_id=str(row["user_id"]),
            rank=int(row["rank"]),
            round=int(row["round"]),
            score=str(row["score"]),
            entry_seq=row.get("entry_seq"),
            entry_id=row.get("entry_id"),
            draw_id=str(row["draw_id"]),
            server_seed=str(row["server_seed"]),
            seed_commitment=str(row["seed_commitment"]),
            awarded_at=int(row.get("awarded_at") or 0),
        )
        for row in db.query(sql, params)
    ]


def winners_for_user(db: Database, user_id: str, *, limit: int = 25) -> list[dict[str, Any]]:
    return db.query(
        """
        SELECT w.giveaway_id, g.title, g.guild_id, w.rank, w.round, w.awarded_at, w.score
        FROM giveaway_winners w
        JOIN giveaways g ON g.id = w.giveaway_id
        WHERE w.user_id = ?
        ORDER BY w.awarded_at DESC
        LIMIT ?
        """,
        (user_id, max(1, min(limit, 100))),
    )


def draw_count(db: Database, giveaway_id: str) -> int:
    return int(db.scalar("SELECT COUNT(*) FROM giveaway_draws WHERE giveaway_id = ?", (giveaway_id,)) or 0)


def manifest_payload(draw: DrawRecord) -> dict[str, Any]:
    try:
        return json.loads(draw.manifest_json)
    except json.JSONDecodeError:  # pragma: no cover - corrupted row
        return {}