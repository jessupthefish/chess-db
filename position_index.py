"""
Full-game position index for /search/position — one Zobrist hash per
(game, ply), every ply of every standard-rules game.

Mirrors opening_tree.py's role: cheap PGN parsing + bulk inserts, no engine
calls, so the project's no-batch-analysis rule doesn't apply (that rule is
about Stockfish). Unlike the opening tree this has no ply cap and no
tree structure — it's a flat hash index sized for "find every game that
reached this position", including middlegames and endgames.

Hash: chess.polyglot.zobrist_hash(board) — keyed on pieces + turn +
castling + legal en passant, i.e. the same "same position" semantics as the
opening tree's epd() key (slightly stricter on ep, which is correct).
Stored as signed 64-bit for SQLite. Lookups verify candidates by replaying
the PGN (collision guard in app.py), so a rare hash collision costs a
false candidate, never a wrong result.

Entry points, same shape as opening_tree:
- ingest_game(game): index one game; idempotent (skips if (game_id, 0) row
  exists).
- rebuild_all(): backfill every un-indexed game; called by
  `flask build-position-index` and after every sync/import alongside
  opening_tree.rebuild_all() — cheap since it only processes new games.
"""

from __future__ import annotations

import io
import logging

import chess
import chess.pgn
import chess.polyglot

from models import Game, PositionHash, db
from opening_tree import STANDARD_RULES

log = logging.getLogger("position_index")

COMMIT_EVERY = 200  # games per commit during a full rebuild


def _signed64(h: int) -> int:
    """Polyglot hashes are unsigned 64-bit; SQLite integers are signed."""
    return h - (1 << 64) if h >= (1 << 63) else h


def ingest_game(game: Game, commit: bool = True) -> bool:
    """Index one game's positions. Returns True if rows were written."""
    if (game.rules or "chess") not in STANDARD_RULES:
        return False
    if db.session.get(PositionHash, (game.game_id, 0)) is not None:
        return False  # already indexed

    try:
        pgn_game = chess.pgn.read_game(io.StringIO(game.pgn))
        if pgn_game is None:
            return False
        board = pgn_game.board()
        if board.fen() != chess.STARTING_FEN:
            return False  # custom start position — can't index meaningfully
        rows = [{
            "game_id": game.game_id,
            "ply": 0,
            "zobrist": _signed64(chess.polyglot.zobrist_hash(board)),
        }]
        for ply, move in enumerate(pgn_game.mainline_moves(), start=1):
            board.push(move)
            rows.append({
                "game_id": game.game_id,
                "ply": ply,
                "zobrist": _signed64(chess.polyglot.zobrist_hash(board)),
            })
    except Exception:
        log.exception("position index: failed to parse game %s", game.game_id)
        return False

    db.session.bulk_insert_mappings(PositionHash, rows)
    if commit:
        db.session.commit()
    return True


def rebuild_all() -> dict:
    """Index every game that isn't indexed yet. Safe to re-run any time."""
    indexed_ids = db.session.query(PositionHash.game_id).filter(PositionHash.ply == 0)
    q = (
        db.session.query(Game.game_id)
        .filter(Game.rules.in_(STANDARD_RULES))
        .filter(~Game.game_id.in_(indexed_ids))
    )
    todo = [gid for (gid,) in q.all()]

    done = failed = 0
    for i, gid in enumerate(todo, start=1):
        game = db.session.get(Game, gid)
        if game is None:
            continue
        if ingest_game(game, commit=False):
            done += 1
        else:
            failed += 1
        if i % COMMIT_EVERY == 0:
            db.session.commit()
            db.session.expunge_all()  # keep memory flat over a 24k-game backfill
        if i % 2000 == 0:
            log.info("position index: %s/%s games", i, len(todo))
    db.session.commit()
    return {"indexed": done, "skipped_or_failed": failed, "todo_was": len(todo)}
