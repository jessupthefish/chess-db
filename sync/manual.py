"""
sync/manual.py — manual PGN import (OTB games, pasted or uploaded PGN files).

Parses PGN text with python-chess (the strict server-side parser — nothing
downstream ever relies on client-side PGN parsing), find-or-creates players
under source='manual' identities, and dedupes with a deterministic content
hash: manual games have no upstream game id, so source_game_id is
sha256(white|black|date|uci-moves)[:32] under the existing
(source, source_game_id) unique constraint. Re-importing the same PGN is a
counted no-op.
"""

from __future__ import annotations

import hashlib
import io
import logging
from datetime import datetime

import chess.pgn
from sqlalchemy import func

from models import Game, Player, PlayerIdentity, db
from sync.common import upsert_opening

log = logging.getLogger("sync.manual")


def _resolve_manual_player(name: str) -> Player:
    """Find-or-create a Player for a manually imported game.

    First tries to link to an existing player: case-insensitive match on any
    identity username (so OTB games under your online handle attach to you)
    or exact display-name match (so 'Magnus Carlsen' hits the broadcast
    player). No fuzzy matching beyond that — flask link-identity exists for
    corrections. Otherwise creates a Player + a source='manual' identity,
    mirroring sync/chesscom.py:_resolve_player.
    """
    name = name.strip()
    ident = PlayerIdentity.query.filter(
        func.lower(PlayerIdentity.username) == name.lower()
    ).first()
    if ident:
        return ident.player

    player = Player.query.filter(Player.display_name == name).first()
    if player:
        return player

    player = Player(display_name=name, is_self=False, is_friend=False)
    db.session.add(player)
    db.session.flush()
    db.session.add(
        PlayerIdentity(player_id=player.player_id, source="manual", username=name)
    )
    return player


def _infer_time_class(time_control: str | None) -> str | None:
    """Map a PGN TimeControl header (e.g. '300+2', '5400+30', '-') to the
    chess.com-style time_class buckets the rest of the app filters on."""
    if not time_control or time_control in ("-", "?"):
        return None
    base = time_control.split("+", 1)[0]
    try:
        seconds = int(base.split("/")[-1])  # '40/7200' style: use the period
    except ValueError:
        return None
    if seconds < 180:
        return "bullet"
    if seconds < 600:
        return "blitz"
    if seconds < 1800:
        return "rapid"
    return "classical"


def _parse_played_at(headers) -> datetime | None:
    """UTCDate/UTCTime preferred, else Date (PGN '????.??.??' tolerated)."""
    date = headers.get("UTCDate") or headers.get("Date") or ""
    time_ = headers.get("UTCTime") or headers.get("Time") or "00:00:00"
    date = date.replace("??", "01").replace(".", "-")
    try:
        return datetime.fromisoformat(f"{date} {time_}")
    except ValueError:
        return None


def _content_hash(white: str, black: str, date: str, moves: list[str]) -> str:
    payload = f"{white.lower()}|{black.lower()}|{date}|{' '.join(moves)}"
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


def import_pgn_text(text: str) -> dict:
    """Import every game in a PGN string. Returns counts:
    {'new_games', 'duplicates', 'skipped'}. Commits once at the end."""
    new_games = duplicates = skipped = 0
    stream = io.StringIO(text)

    while True:
        try:
            pgn_game = chess.pgn.read_game(stream)
        except Exception:
            skipped += 1
            continue
        if pgn_game is None:
            break

        headers = pgn_game.headers
        # variants / custom start positions can't be replayed from the
        # standard start — skip them, same policy as opening_tree.py
        variant = headers.get("Variant", "Standard").lower()
        if variant not in ("standard", "chess") or headers.get("SetUp") == "1" or "FEN" in headers:
            skipped += 1
            continue
        if pgn_game.errors:
            skipped += 1
            continue

        white_name = headers.get("White", "").strip()
        black_name = headers.get("Black", "").strip()
        if not white_name or not black_name or white_name == "?" or black_name == "?":
            skipped += 1
            continue

        moves = [m.uci() for m in pgn_game.mainline_moves()]
        if not moves:
            skipped += 1
            continue

        source_game_id = _content_hash(
            white_name, black_name, headers.get("Date", "?"), moves
        )
        if Game.query.filter_by(source="manual", source_game_id=source_game_id).first():
            duplicates += 1
            continue

        white = _resolve_manual_player(white_name)
        black = _resolve_manual_player(black_name)
        opening = upsert_opening(headers.get("ECO"), headers.get("Opening"))

        def _rating(key):
            v = headers.get(key, "")
            return int(v) if v.isdigit() else None

        time_control = headers.get("TimeControl")
        game = Game(
            source="manual",
            source_game_id=source_game_id,
            white_id=white.player_id,
            black_id=black.player_id,
            white_rating=_rating("WhiteElo"),
            black_rating=_rating("BlackElo"),
            result=headers.get("Result") if headers.get("Result") != "*" else None,
            termination=headers.get("Termination"),
            rules="chess",
            time_class=_infer_time_class(time_control),
            time_control=time_control if time_control not in ("-", "?") else None,
            played_at=_parse_played_at(headers),
            ply_count=len(moves),
            pgn=str(pgn_game),
            opening_id=opening.opening_id if opening else None,
        )
        db.session.add(game)
        new_games += 1

    db.session.commit()
    log.info("manual import: %s new, %s duplicates, %s skipped", new_games, duplicates, skipped)
    return {"new_games": new_games, "duplicates": duplicates, "skipped": skipped}
