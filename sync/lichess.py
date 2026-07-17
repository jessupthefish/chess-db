"""
Lichess sync — fetches games via the "export games of a user" API in PGN
format and writes to the SQLAlchemy models. Mirrors sync/chesscom.py.
"""

from __future__ import annotations

import io
import logging
import re
from datetime import datetime, timezone
from typing import Optional

import chess.pgn
import requests
from sqlalchemy import func

from models import ArchiveCache, Event, Game, Player, PlayerIdentity, db
from sync.common import fetch_with_retry, upsert_opening

API_BASE = "https://lichess.org/api"
USER_AGENT = "chess-db-sync/2.0 (contact: you@stevenjessup.com)"
TIMEOUT = 60
RETRIES = (1, 3, 10)

log = logging.getLogger("sync.lichess")

# ── HTTP client ──────────────────────────────────────────────────────────

_session: Optional[requests.Session] = None


def _get_session() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept-Encoding": "gzip",
        })
    return _session


def _fetch(url: str, extra_headers: dict | None = None) -> Optional[requests.Response]:
    return fetch_with_retry(_get_session(), url, extra_headers, TIMEOUT, RETRIES)


# ── Player resolution ────────────────────────────────────────────────────

def _resolve_player(username: str, profile: dict | None = None) -> Player:
    """Find-or-create a Player from a Lichess username.

    Lichess usernames are case-insensitive, so identity lookups must be too —
    see the equivalent chesscom.py note (same class of bug bit that sync).
    """
    ident = PlayerIdentity.query.filter(
        PlayerIdentity.source == "lichess",
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
        display = profile.get("username") or username
        title = profile.get("title")

    player = Player(display_name=display, title=title, is_self=False, is_friend=False)
    db.session.add(player)
    db.session.flush()

    db.session.add(PlayerIdentity(
        player_id=player.player_id, source="lichess", username=username
    ))
    return player


def _update_player_from_profile(player: Player, profile: dict) -> None:
    player.display_name = profile.get("username") or player.display_name
    player.title = profile.get("title") or player.title
    country = (profile.get("profile") or {}).get("flag")
    player.country = country or player.country


# ── Event upsert ─────────────────────────────────────────────────────────

# Lichess's Event header for ordinary games is just a description like
# "Rated Blitz game" / "Casual Bullet game" — not a real tournament/event.
_GENERIC_EVENT_RE = re.compile(r"^(rated|casual) [\w ]+ game$", re.IGNORECASE)


def _upsert_event(name: str | None) -> Event | None:
    if not name or _GENERIC_EVENT_RE.match(name):
        return None
    existing = Event.query.filter_by(name=name, source="lichess").first()
    if existing:
        return existing
    event = Event(name=name, source="lichess")
    db.session.add(event)
    db.session.flush()
    return event


# ── Game parsing ─────────────────────────────────────────────────────────

def _time_class_from_control(tc: str | None) -> str | None:
    """Classify by Lichess's own speed buckets (estimated seconds = base + 40*increment).

    Event header isn't reliable for this — tournament games carry the arena/swiss
    name there (e.g. "Take Take Take Arena") instead of "Rated Blitz game".
    """
    if not tc:
        return None
    if tc == "-" or "/" in tc:
        return "correspondence"
    if "+" not in tc:
        return None
    try:
        base, inc = tc.split("+")
        total = int(base) + 40 * int(inc)
    except ValueError:
        return None
    if total < 180:
        return "bullet"
    if total < 480:
        return "blitz"
    if total < 1500:
        return "rapid"
    return "classical"


def _game_id_from_site(site: str | None) -> str | None:
    if not site:
        return None
    path = site.rstrip("/").rsplit("/", 1)[-1]
    # some export variants suffix a color, e.g. .../abcd1234/black
    return path


def _parse_datetime(headers: dict) -> datetime | None:
    date = headers.get("UTCDate") or headers.get("Date")
    time_ = headers.get("UTCTime")
    if not date or date == "????.??.??":
        return None
    try:
        if time_:
            return datetime.strptime(f"{date} {time_}", "%Y.%m.%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return datetime.strptime(date, "%Y.%m.%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _elo(headers: dict, key: str) -> int | None:
    val = headers.get(key)
    if not val or not val.isdigit():
        return None
    return int(val)


def _parse_and_insert(game_obj: chess.pgn.Game) -> bool:
    """Parse one python-chess Game object, insert if new. Returns True if inserted."""
    headers = game_obj.headers

    source_id = headers.get("GameId") or _game_id_from_site(headers.get("Site"))
    if not source_id:
        return False

    existing = Game.query.filter_by(source="lichess", source_game_id=source_id).first()
    if existing:
        return False

    white_user = headers.get("White")
    black_user = headers.get("Black")
    if not white_user or not black_user:
        return False

    white = _resolve_player(white_user)
    black = _resolve_player(black_user)

    opening = upsert_opening(headers.get("ECO"), headers.get("Opening"), None)
    event = _upsert_event(headers.get("Event"))

    ply_count = sum(1 for _ in game_obj.mainline_moves())
    played_at = _parse_datetime(headers)
    termination = headers.get("Termination")
    time_class = _time_class_from_control(headers.get("TimeControl"))
    variant = (headers.get("Variant") or "Standard").lower()

    exporter = chess.pgn.StringExporter(headers=True, variations=False, comments=False)
    pgn_text = game_obj.accept(exporter)

    game = Game(
        source="lichess",
        source_game_id=source_id,
        source_url=f"https://lichess.org/{source_id}",
        white_id=white.player_id,
        black_id=black.player_id,
        white_rating=_elo(headers, "WhiteElo"),
        black_rating=_elo(headers, "BlackElo"),
        result=headers.get("Result", "*"),
        termination=termination.lower() if termination else None,
        rules=variant,
        time_class=time_class,
        time_control=headers.get("TimeControl", ""),
        played_at=played_at,
        ply_count=ply_count,
        pgn=pgn_text,
        opening_id=opening.opening_id if opening else None,
        event_id=event.event_id if event else None,
    )
    db.session.add(game)
    return True


# ── Top-level sync ───────────────────────────────────────────────────────

_CACHE_KEY_PREFIX = "lichess:"


def sync_user(username: str) -> dict:
    """Sync a Lichess user. Returns stats dict."""
    log.info("syncing lichess/%s", username)

    profile_resp = _fetch(f"{API_BASE}/user/{username}")
    if profile_resp is None or profile_resp.status_code == 404:
        return {"error": f"player '{username}' not found"}
    profile = profile_resp.json()

    player = _resolve_player(username, profile=profile)
    db.session.commit()

    cache_key = f"{_CACHE_KEY_PREFIX}{username.lower()}"
    cached = ArchiveCache.query.get(cache_key)

    params = "opening=true&clocks=false&evals=false"
    if cached and cached.last_modified:
        params += f"&since={cached.last_modified}"
    url = f"{API_BASE}/games/user/{username}?{params}"

    sess = _get_session()
    resp = sess.get(
        url,
        headers={"Accept": "application/x-chess-pgn"},
        timeout=TIMEOUT,
        stream=True,
    )
    if resp.status_code != 200:
        return {"error": f"lichess export failed with status {resp.status_code}"}

    resp.raw.decode_content = True
    stream = io.TextIOWrapper(resp.raw, encoding="utf-8")

    total_games = 0
    total_new = 0
    max_end_ms = int(cached.last_modified) if (cached and cached.last_modified) else 0

    while True:
        game_obj = chess.pgn.read_game(stream)
        if game_obj is None:
            break
        total_games += 1
        if _parse_and_insert(game_obj):
            total_new += 1

        played_at = _parse_datetime(game_obj.headers)
        if played_at:
            end_ms = int(played_at.timestamp() * 1000)
            max_end_ms = max(max_end_ms, end_ms)

        if total_games % 200 == 0:
            db.session.commit()
            log.info("  ...%d games processed (%d new)", total_games, total_new)

    entry = cached or ArchiveCache(archive_url=cache_key)
    entry.last_modified = str(max_end_ms) if max_end_ms else entry.last_modified
    entry.fetched_at = int(datetime.now(timezone.utc).timestamp())
    if not cached:
        db.session.add(entry)
    db.session.commit()

    log.info("lichess/%s: %d games seen, %d new", username, total_games, total_new)

    return {
        "username": username,
        "player_id": player.player_id,
        "total_games": total_games,
        "new_games": total_new,
    }
