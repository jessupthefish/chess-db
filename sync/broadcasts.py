"""
Lichess broadcast sync — fetches major tournament games (Candidates, World
Championship, Tata Steel, Norway Chess, Grand Chess Tour, etc.) via the
broadcast API and writes to the SQLAlchemy models. Mirrors sync/lichess.py's
PGN-streaming pattern.

Players here are identified by FIDE ID (WhiteFideId/BlackFideId PGN headers),
not a chess.com/Lichess username — a different identity path from the other
two sync modules (PlayerIdentity.source = "fide").
"""

from __future__ import annotations

import io
import logging
from datetime import datetime, timezone

import chess.pgn
import requests

from models import Event, Game, Player, PlayerIdentity, db
from sync.common import fetch_with_retry, upsert_opening

API_BASE = "https://lichess.org/api/broadcast"
USER_AGENT = "chess-db-sync/2.0 (contact: you@stevenjessup.com)"
TIMEOUT = 60
RETRIES = (1, 3, 10)

log = logging.getLogger("sync.broadcasts")

_session: requests.Session | None = None


def _get_session() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers.update({"User-Agent": USER_AGENT})
    return _session


def _fetch(url: str, extra_headers: dict | None = None) -> requests.Response | None:
    return fetch_with_retry(_get_session(), url, extra_headers, TIMEOUT, RETRIES)


def _format_name(raw: str) -> str:
    """Broadcast PGNs use 'Last, First' — reformat to 'First Last' for display."""
    if "," in raw:
        last, _, first = raw.partition(",")
        return f"{first.strip()} {last.strip()}"
    return raw.strip()


def _resolve_player_by_fide(fide_id: str | None, raw_name: str) -> Player:
    """Find-or-create a Player from a FIDE ID (falls back to exact name match
    when a smaller-federation player lacks a FIDE ID in the PGN)."""
    display = _format_name(raw_name)

    if fide_id:
        ident = PlayerIdentity.query.filter_by(source="fide", username=fide_id).first()
        if ident:
            return ident.player
    else:
        existing = Player.query.filter_by(display_name=display).first()
        if existing:
            return existing

    player = Player(display_name=display, is_self=False, is_friend=False)
    db.session.add(player)
    db.session.flush()

    if fide_id:
        db.session.add(PlayerIdentity(player_id=player.player_id, source="fide", username=fide_id))
    return player


def _upsert_event(name: str | None, location: str | None, dates: list[int] | None) -> Event | None:
    if not name:
        return None
    existing = Event.query.filter_by(name=name, source="broadcast").first()
    if existing:
        return existing
    start_date = end_date = None
    if dates:
        start_date = datetime.fromtimestamp(dates[0] / 1000, tz=timezone.utc).date()
        if len(dates) > 1:
            end_date = datetime.fromtimestamp(dates[1] / 1000, tz=timezone.utc).date()
    event = Event(name=name, location=location, start_date=start_date, end_date=end_date, source="broadcast")
    db.session.add(event)
    db.session.flush()
    return event


def _game_id_from_headers(headers) -> str | None:
    game_url = headers.get("GameURL", "")
    return game_url.rstrip("/").rsplit("/", 1)[-1] if game_url else None


def _elo(headers, key: str) -> int | None:
    val = headers.get(key)
    return int(val) if val and val.isdigit() else None


def _parse_and_insert(game_obj: chess.pgn.Game, event: Event | None) -> bool:
    headers = game_obj.headers
    source_id = _game_id_from_headers(headers)
    if not source_id:
        return False

    existing = Game.query.filter_by(source="broadcast", source_game_id=source_id).first()
    if existing:
        return False

    white_name, black_name = headers.get("White"), headers.get("Black")
    if not white_name or not black_name:
        return False

    white = _resolve_player_by_fide(headers.get("WhiteFideId"), white_name)
    black = _resolve_player_by_fide(headers.get("BlackFideId"), black_name)

    if headers.get("WhiteTitle") and not white.title:
        white.title = headers.get("WhiteTitle")
    if headers.get("BlackTitle") and not black.title:
        black.title = headers.get("BlackTitle")

    opening = upsert_opening(headers.get("ECO"), headers.get("Opening"), None)
    ply_count = sum(1 for _ in game_obj.mainline_moves())

    date_str = headers.get("UTCDate") or headers.get("Date")
    time_str = headers.get("UTCTime")
    played_at = None
    if date_str and date_str != "????.??.??":
        try:
            if time_str:
                played_at = datetime.strptime(f"{date_str} {time_str}", "%Y.%m.%d %H:%M:%S").replace(tzinfo=timezone.utc)
            else:
                played_at = datetime.strptime(date_str, "%Y.%m.%d").replace(tzinfo=timezone.utc)
        except ValueError:
            pass

    exporter = chess.pgn.StringExporter(headers=True, variations=False, comments=True)
    pgn_text = game_obj.accept(exporter)

    game = Game(
        source="broadcast",
        source_game_id=source_id,
        source_url=headers.get("GameURL"),
        white_id=white.player_id,
        black_id=black.player_id,
        white_rating=_elo(headers, "WhiteElo"),
        black_rating=_elo(headers, "BlackElo"),
        result=headers.get("Result", "*"),
        termination=headers.get("Termination"),
        rules=(headers.get("Variant") or "Standard").lower(),
        time_class="classical",
        time_control=headers.get("TimeControl", ""),
        played_at=played_at,
        ply_count=ply_count,
        pgn=pgn_text,
        opening_id=opening.opening_id if opening else None,
        event_id=event.event_id if event else None,
    )
    db.session.add(game)
    return True


def sync_tournament(tour_id: str, fallback_name: str = "") -> dict:
    """Sync one broadcast tournament. Returns stats dict."""
    log.info("syncing broadcast tournament %s", tour_id)

    info_resp = _fetch(f"{API_BASE}/{tour_id}")
    tour_info = info_resp.json().get("tour", {}) if info_resp and info_resp.status_code == 200 else {}
    name = tour_info.get("name") or fallback_name
    location = (tour_info.get("info") or {}).get("location")
    dates = tour_info.get("dates")

    event = _upsert_event(name, location, dates)
    db.session.commit()

    resp = _fetch(f"{API_BASE}/{tour_id}.pgn")
    if resp is None or resp.status_code != 200:
        return {"error": f"broadcast '{tour_id}' export failed (status {resp.status_code if resp else 'none'})"}

    stream = io.StringIO(resp.text)
    total, new = 0, 0
    while True:
        game_obj = chess.pgn.read_game(stream)
        if game_obj is None:
            break
        total += 1
        if _parse_and_insert(game_obj, event):
            new += 1
        if total % 50 == 0:
            db.session.commit()
    db.session.commit()

    log.info("%s: %d games seen, %d new", tour_id, total, new)
    return {"tournament_id": tour_id, "name": name, "total_games": total, "new_games": new}


def sync_all() -> list[dict]:
    from broadcast_tournaments import BROADCAST_TOURNAMENTS

    results = []
    for t in BROADCAST_TOURNAMENTS:
        results.append(sync_tournament(t["id"], fallback_name=t["name"]))
    return results
