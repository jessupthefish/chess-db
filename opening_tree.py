"""
Opening explorer tree-building — walks stored PGN text (no engine calls) to
build a position-frequency tree, deduped across transpositions by a
normalized position key. Schema is OpeningNode/GamePosition in models.py.

Distinct from analysis.py: this is cheap PGN parsing + aggregation, not
Stockfish. The project's "no batch/background analysis" rule (see
BUILD_LOG.md) is specifically about engine calls, which are expensive and
were explicitly ruled out for bulk use. A one-time backfill over the whole
game library here (`flask build-opening-tree`) involves no engine and is
fine.

Two entry points:
- ingest_game(game): walk one game's PGN, upsert nodes + one GamePosition
  row per ply (capped at MAX_PLY). Idempotent — safe to call again for a
  game that's already been ingested (no-op).
- rebuild_all(): full backfill, skips games already ingested. Called by
  `flask build-opening-tree` and after every sync (chesscom/lichess/pros/
  broadcasts) so newly-synced games get picked up without a separate manual
  step — cheap since it only processes the new games each time.
"""

from __future__ import annotations

import io
import logging

import chess
import chess.pgn

log = logging.getLogger("opening_tree")

MAX_PLY = 40                 # 20 full moves — bounds the tree to "opening" territory
OPENING_LABEL_MAX_PLY = 16    # only auto-label nodes this shallow with a named opening
COMMIT_EVERY = 200           # games per commit during a full rebuild


def normalize_key(board: chess.Board) -> str:
    """Position key that merges transpositions: board + turn + castling + en
    passant target, deliberately excluding halfmove/fullmove counters."""
    return board.epd()


def _preload_cache():
    from models import OpeningNode

    return {n.fen_key: n for n in OpeningNode.query.all()}


# Rules values seen from the two sync sources that mean "ordinary chess from
# the standard starting position" (chess.com: "chess", Lichess: "standard" —
# its Variant header defaults to "Standard"). Anything else (chess960,
# oddschess, atomic, etc.) starts from a different position or army, which
# would make replaying moves from chess.Board() either raise an illegal-move
# assertion or silently produce a wrong FEN — so those are skipped entirely.
STANDARD_RULES = {"chess", "standard"}


def _get_or_create_node(cache, fen_key, fen, parent, move_san, move_uci, ply, new_keys):
    from models import OpeningNode, db

    node = cache.get(fen_key)
    if node is not None:
        return node

    node = OpeningNode(
        parent_id=parent.node_id if parent else None,
        ply=ply,
        fen_key=fen_key,
        fen=fen,
        move_san=move_san,
        move_uci=move_uci,
    )
    db.session.add(node)
    db.session.flush()  # need node.node_id to use as the next ply's parent_id
    cache[fen_key] = node
    new_keys.append(fen_key)
    return node


def ingest_game(game, cache=None, commit=True) -> bool:
    """Walk one game's stored PGN, upserting OpeningNode rows and one
    GamePosition row per ply (capped at MAX_PLY). Returns False (no-op) if
    this game_id was already ingested, its PGN doesn't parse, or it's not a
    standard-chess game (see STANDARD_RULES). On any other failure partway
    through, evicts whatever nodes this call added to `cache` before
    re-raising, so a caller's rollback (which discards this game's DB rows)
    doesn't leave the in-memory cache pointing at now-nonexistent rows."""
    from models import GamePosition, db

    if game.rules not in STANDARD_RULES:
        return False

    if GamePosition.query.filter_by(game_id=game.game_id, ply=0).first():
        return False

    if cache is None:
        cache = _preload_cache()

    game_obj = chess.pgn.read_game(io.StringIO(game.pgn))
    if game_obj is None:
        return False

    new_keys = []
    try:
        board = chess.Board()
        node = _get_or_create_node(cache, normalize_key(board), board.fen(), None, None, None, 0, new_keys)
        db.session.add(GamePosition(game_id=game.game_id, ply=0, node_id=node.node_id))

        for ply, move in enumerate(game_obj.mainline_moves(), start=1):
            if ply > MAX_PLY:
                break
            san = board.san(move)
            uci = move.uci()
            board.push(move)
            node = _get_or_create_node(cache, normalize_key(board), board.fen(), node, san, uci, ply, new_keys)
            db.session.add(GamePosition(game_id=game.game_id, ply=ply, node_id=node.node_id))

            if node.opening_id is None and game.opening_id is not None and ply <= OPENING_LABEL_MAX_PLY:
                node.opening_id = game.opening_id
    except Exception:
        for k in new_keys:
            cache.pop(k, None)
        raise

    if commit:
        db.session.commit()
    return True


def rebuild_all() -> dict:
    """Full backfill over every Game not yet in the tree. The entry point for
    `flask build-opening-tree`, and re-run (cheaply — it skips already-done
    games) after every sync so new games are picked up automatically."""
    from models import Game, GamePosition, db

    cache = _preload_cache()
    already = {
        gid for (gid,) in db.session.query(GamePosition.game_id).filter_by(ply=0).distinct()
    }

    ingested = 0
    failed = 0
    since_commit = 0
    for game in Game.query.yield_per(COMMIT_EVERY):
        if game.game_id in already:
            continue
        try:
            # A SAVEPOINT per game: if this one game raises partway through,
            # only its own uncommitted rows roll back — not the whole batch
            # of other games already flushed since the last commit() below.
            with db.session.begin_nested():
                ok = ingest_game(game, cache=cache, commit=False)
        except Exception:
            log.exception("failed to ingest game %s into opening tree", game.game_id)
            failed += 1
            continue

        if ok:
            ingested += 1
            since_commit += 1

        if since_commit >= COMMIT_EVERY:
            db.session.commit()
            since_commit = 0
            log.info("opening tree: ingested %d games so far", ingested)

    db.session.commit()
    return {"ingested": ingested, "failed": failed, "nodes": len(cache), "skipped": len(already)}
