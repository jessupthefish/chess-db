"""
On-demand Stockfish analysis — strictly user-triggered, no batch/background
pre-analysis of the game library. Two entry points:

- run_full_analysis(app, game_id): walks an entire game ply-by-ply, called from
  a background thread kicked off by POST /games/<id>/analyze. Stores every ply
  (not just blunders) so the live move-suggestion cache lookup (Phase 3) can
  find any position, not just notable ones.
- analyze_position(fen, stockfish_path, movetime): single-position lookup for
  the live move-suggestion toggle, spawn-and-close, never persisted.
"""

from __future__ import annotations

import io
import logging
from datetime import datetime, timezone

import chess
import chess.engine
import chess.pgn

log = logging.getLogger("analysis")

MOVETIME = 0.4  # seconds per position

# cp_loss thresholds (standard-ish values used by most casual analysis tools)
BLUNDER_CP = 200
MISTAKE_CP = 100
INACCURACY_CP = 50
MATE_SCORE = 100000  # numeric stand-in for "mate", scaled by python-chess's Score.score(mate_score=...)


def classify(cp_loss: int) -> str | None:
    if cp_loss > BLUNDER_CP:
        return "blunder"
    if cp_loss > MISTAKE_CP:
        return "mistake"
    if cp_loss > INACCURACY_CP:
        return "inaccuracy"
    return None


LINE_PREVIEW_PLIES = 6  # ~3 full moves, lichess-style preview length


def _build_preview(board: chess.Board, pv: list[chess.Move]) -> list[str]:
    b = board.copy()
    sans = []
    for mv in pv[:LINE_PREVIEW_PLIES]:
        sans.append(b.san(mv))
        b.push(mv)
    return sans


def _eval_position_multi(
    engine: chess.engine.SimpleEngine, board: chess.Board, movetime: float = MOVETIME, multipv: int = 3
) -> list[dict]:
    """Evaluate one position, up to `multipv` candidate lines (white-POV score/mate,
    engine's best move, and a short SAN preview of the line). Returns fewer than
    `multipv` entries if the engine can't produce that many (e.g. near-forced positions)."""
    if board.is_game_over():
        # Terminal position (checkmate/stalemate/etc) — no move to suggest, and
        # the mate/draw result itself is the "score" rather than a search result.
        if board.is_checkmate():
            # side to move is mated; the mate is a win for the other side
            mate_in = -1 if board.turn == chess.WHITE else 1
            return [{"rank": 1, "score_cp": None, "mate_in": mate_in, "best_uci": None, "best_san": None,
                      "pv_uci": [], "pv_san": []}]
        return [{"rank": 1, "score_cp": 0, "mate_in": None, "best_uci": None, "best_san": None,
                  "pv_uci": [], "pv_san": []}]

    infos = engine.analyse(board, chess.engine.Limit(time=movetime), multipv=multipv)
    if isinstance(infos, dict):
        infos = [infos]
    lines = []
    for i, info in enumerate(infos):
        white_score = info["score"].white()
        pv = info.get("pv", [])
        best_move = pv[0] if pv else None
        lines.append({
            "rank": i + 1,
            "score_cp": white_score.score(),  # None if this is a mate line
            "mate_in": white_score.mate(),    # None if not a mate line
            "best_uci": best_move.uci() if best_move else None,
            "best_san": board.san(best_move) if best_move else None,
            "pv_uci": [m.uci() for m in pv[:LINE_PREVIEW_PLIES]],
            "pv_san": _build_preview(board, pv),
            "_numeric": white_score.score(mate_score=MATE_SCORE),  # for cp_loss math, never stored
        })
    return lines


def run_full_analysis(app, game_id: int) -> None:
    """Background-thread target. Must push its own app context (called from a thread,
    not a request). Opens one engine instance for the whole game, closes it at the end."""
    from models import Game, GameAnalysis, MoveEval, MoveEvalLine, db

    with app.app_context():
        game = Game.query.get(game_id)
        row = GameAnalysis.query.get(game_id)
        if not game or not row:
            return

        try:
            game_obj = chess.pgn.read_game(io.StringIO(game.pgn))
            if game_obj is None:
                raise ValueError("could not parse stored PGN")
            moves = list(game_obj.mainline_moves())

            board = chess.Board()
            positions = [board.copy()]
            for move in moves:
                board.push(move)
                positions.append(board.copy())

            row.ply_total = len(positions) - 1
            db.session.commit()

            engine = chess.engine.SimpleEngine.popen_uci(app.config["STOCKFISH_PATH"])
            try:
                evals = []
                for i, pos in enumerate(positions):
                    evals.append(_eval_position_multi(engine, pos))
                    row.plies_done = i + 1
                    if i % 5 == 0:
                        db.session.commit()

                for i, lines in enumerate(evals):
                    for line in lines:
                        db.session.add(MoveEvalLine(
                            game_id=game_id,
                            ply=i,
                            rank=line["rank"],
                            score_cp=line["score_cp"],
                            mate_in=line["mate_in"],
                            best_move_uci=line["best_uci"],
                            best_move_san=line["best_san"],
                            pv_san=" ".join(line["pv_san"]),
                        ))

                white_losses, black_losses = [], []
                for i in range(1, len(positions)):
                    before, after = evals[i - 1][0], evals[i][0]
                    mover_white = positions[i - 1].turn == chess.WHITE
                    before_n = before.get("_numeric", 0) or 0
                    after_n = after.get("_numeric", 0) or 0
                    cp_loss = (before_n - after_n) if mover_white else (after_n - before_n)
                    cp_loss = max(0, cp_loss)
                    (white_losses if mover_white else black_losses).append(cp_loss)

                    db.session.add(MoveEval(
                        game_id=game_id,
                        ply=i,
                        score_cp=after["score_cp"],
                        mate_in=after["mate_in"],
                        best_move_uci=before["best_uci"],
                        best_move_san=before["best_san"],
                        classification=classify(cp_loss),
                    ))
                db.session.commit()
            finally:
                engine.quit()

            row.white_acpl = sum(white_losses) / len(white_losses) if white_losses else None
            row.black_acpl = sum(black_losses) / len(black_losses) if black_losses else None
            row.analyzed_at = datetime.now(timezone.utc)
            db.session.commit()
            log.info("analysis done: game %s (%d plies)", game_id, len(positions) - 1)
        except Exception as exc:  # noqa: BLE001 - must not leave the row stuck NULL
            log.exception("analysis failed: game %s", game_id)
            row.error = str(exc)[:500]
            db.session.commit()


def analyze_position(fen: str, stockfish_path: str, movetime: float = MOVETIME) -> list[dict]:
    """Single-position, ephemeral, spawn-and-close. Used by the live move-suggestion toggle."""
    board = chess.Board(fen)
    with chess.engine.SimpleEngine.popen_uci(stockfish_path) as engine:
        lines = _eval_position_multi(engine, board, movetime)
    for line in lines:
        line.pop("_numeric", None)
    return lines
