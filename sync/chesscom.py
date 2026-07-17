"""
Chess.com sync — fetches profiles + game archives via the Published-Data API
and writes to the SQLAlchemy models.
"""

from __future__ import annotations

import io
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import chess.pgn
import requests
from sqlalchemy import func

from models import ArchiveCache, Event, Game, Player, PlayerIdentity, db
from sync.common import fetch_with_retry, upsert_opening

API_BASE = "https://api.chess.com/pub"
USER_AGENT = "chess-db-sync/2.0 (contact: you@stevenjessup.com)"
TIMEOUT = 30
RETRIES = (1, 3, 10)

log = logging.getLogger("sync.chesscom")

# ── HTTP client ──────────────────────────────────────────────────────────

_session: Optional[requests.Session] = None


def _get_session() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
        })
    return _session


def _fetch(url: str, extra_headers: dict | None = None) -> Optional[requests.Response]:
    return fetch_with_retry(_get_session(), url, extra_headers, TIMEOUT, RETRIES)


# ── Player resolution ────────────────────────────────────────────────────

def _resolve_player(username: str, profile: dict | None = None) -> Player:
    """Find-or-create a Player from a chess.com username.

    Chess.com usernames are case-insensitive, so identity lookups must be too —
    otherwise the same account synced with different casing (e.g. from a profile
    fetch vs. an opponent field) creates duplicate Player rows.
    """
    ident = PlayerIdentity.query.filter(
        PlayerIdentity.source == "chesscom",
        func.lower(PlayerIdentity.username) == username.lower(),
    ).first()
    if ident:
        player = ident.player
        if profile:
            _update_player_from_profile(player, profile)
        return player

    display = username
    title = None
    if profile:
        display = profile.get("name") or username
        title = profile.get("title")

    player = Player(display_name=display, title=title, is_self=False, is_friend=False)
    db.session.add(player)
    db.session.flush()

    db.session.add(PlayerIdentity(
        player_id=player.player_id, source="chesscom", username=username
    ))
    return player


def _update_player_from_profile(player: Player, profile: dict) -> None:
    player.display_name = profile.get("name") or player.display_name
    player.title = profile.get("title") or player.title
    country_url = profile.get("country") or ""
    player.country = country_url.rsplit("/", 1)[-1] if country_url else player.country


def _username_from_id(at_id: str | None) -> str:
    return at_id.rsplit("/", 1)[-1] if at_id else ""


# ── Opening / Event upsert ───────────────────────────────────────────────

def _upsert_event(name: str | None, site: str | None, tournament_url: str | None) -> Event | None:
    if not name or name in ("Live Chess", "-"):
        return None
    if tournament_url:
        existing = Event.query.filter_by(name=name).first()
        if existing:
            return existing
    event = Event(name=name, location=site, source="chesscom")
    db.session.add(event)
    db.session.flush()
    return event


# ── Game parsing ─────────────────────────────────────────────────────────

TIME_CLASS_MAP = {
    "daily": "daily",
    "rapid": "rapid",
    "blitz": "blitz",
    "bullet": "bullet",
}


def _parse_and_insert(api_game: dict) -> bool:
    """Parse one chess.com API game object, insert if new. Returns True if inserted."""
    pgn_text = api_game.get("pgn")
    if not pgn_text:
        return False

    game_url = api_game.get("url", "")
    source_id = game_url.rsplit("/", 1)[-1] if game_url else None
    if not source_id:
        return False

    existing = Game.query.filter_by(source="chesscom", source_game_id=source_id).first()
    if existing:
        return False

    game_obj = chess.pgn.read_game(io.StringIO(pgn_text))
    headers = game_obj.headers if game_obj else {}

    white_data = api_game.get("white", {})
    black_data = api_game.get("black", {})
    white_user = white_data.get("username") or _username_from_id(white_data.get("@id"))
    black_user = black_data.get("username") or _username_from_id(black_data.get("@id"))

    white = _resolve_player(white_user)
    black = _resolve_player(black_user)

    eco_url = api_game.get("eco")
    eco_code = headers.get("ECO")
    opening_name = None
    if eco_url:
        opening_name = eco_url.rsplit("/", 1)[-1].replace("-", " ")
    opening = upsert_opening(eco_code, opening_name, eco_url)

    event = _upsert_event(
        headers.get("Event"),
        headers.get("Site"),
        headers.get("Tournament"),
    )

    # Termination: the loser/drawer's result string tells the story
    termination = None
    for r in (white_data.get("result"), black_data.get("result")):
        if r and r != "win":
            termination = r
            break

    ply_count = sum(1 for _ in game_obj.mainline_moves()) if game_obj else None

    end_ts = api_game.get("end_time")
    played_at = datetime.fromtimestamp(end_ts, tz=timezone.utc) if end_ts else None

    time_class_raw = api_game.get("time_class", "")
    time_class = TIME_CLASS_MAP.get(time_class_raw, time_class_raw)

    game = Game(
        source="chesscom",
        source_game_id=source_id,
        source_url=game_url,
        white_id=white.player_id,
        black_id=black.player_id,
        white_rating=white_data.get("rating"),
        black_rating=black_data.get("rating"),
        result=headers.get("Result", "*"),
        termination=termination,
        rules=api_game.get("rules", "chess"),
        time_class=time_class,
        time_control=api_game.get("time_control", ""),
        played_at=played_at,
        ply_count=ply_count,
        pgn=pgn_text,
        opening_id=opening.opening_id if opening else None,
        event_id=event.event_id if event else None,
    )
    db.session.add(game)
    return True


# ── Archive-level sync ───────────────────────────────────────────────────

def _sync_archive(archive_url: str) -> tuple[int, int]:
    """Fetch one monthly archive. Returns (total, inserted)."""
    cached = ArchiveCache.query.get(archive_url)
    etag = cached.etag if cached else None
    last_mod = cached.last_modified if cached else None

    req_headers = {}
    if etag:
        req_headers["If-None-Match"] = etag
    if last_mod:
        req_headers["If-Modified-Since"] = last_mod

    resp = _fetch(archive_url, req_headers)
    if resp is None:
        return 0, 0
    if resp.status_code == 304:
        log.info("  %s → 304 (cached)", archive_url.split("/pub/")[-1])
        return 0, 0
    if resp.status_code == 404:
        return 0, 0
    resp.raise_for_status()

    data = resp.json()
    games = data.get("games", [])
    inserted = 0
    for g in games:
        if _parse_and_insert(g):
            inserted += 1

    # Update cache
    entry = cached or ArchiveCache(archive_url=archive_url)
    entry.etag = resp.headers.get("ETag")
    entry.last_modified = resp.headers.get("Last-Modified")
    entry.fetched_at = int(time.time())
    if not cached:
        db.session.add(entry)

    db.session.commit()
    log.info("  %s → %d games (%d new)", archive_url.split("/pub/")[-1], len(games), inserted)
    return len(games), inserted


# ── Top-level sync ───────────────────────────────────────────────────────

def sync_user(username: str) -> dict:
    """Sync a chess.com user. Returns stats dict."""
    log.info("syncing chess.com/%s", username)

    resp = _fetch(f"{API_BASE}/player/{username}")
    if resp is None or resp.status_code == 404:
        return {"error": f"player '{username}' not found"}
    profile = resp.json()

    player = _resolve_player(username, profile=profile)
    db.session.commit()

    archives_resp = _fetch(f"{API_BASE}/player/{username}/games/archives")
    archive_urls = (archives_resp.json() if archives_resp else {}).get("archives", [])
    log.info("%s: %d monthly archives", username, len(archive_urls))

    total_games = 0
    total_new = 0
    for url in archive_urls:
        count, new = _sync_archive(url)
        total_games += count
        total_new += new

    return {
        "username": username,
        "player_id": player.player_id,
        "archives": len(archive_urls),
        "total_games": total_games,
        "new_games": total_new,
    }
