"""Shared helpers for source-specific sync modules (chesscom.py, lichess.py)."""

from __future__ import annotations

import logging
import time
from typing import Optional

import requests

from models import Opening, db

log = logging.getLogger("sync.common")


def fetch_with_retry(
    session: requests.Session,
    url: str,
    extra_headers: dict | None = None,
    timeout: int = 30,
    retries: tuple[int, ...] = (1, 3, 10),
) -> Optional[requests.Response]:
    """GET a URL, retrying on network errors and transient HTTP status codes."""
    for attempt, wait in enumerate([0, *retries]):
        if wait:
            time.sleep(wait)
        try:
            resp = session.get(url, timeout=timeout, headers=extra_headers or {})
        except requests.RequestException as exc:
            log.warning("net error %s (attempt %d): %s", url, attempt, exc)
            continue
        if resp.status_code in (429, 500, 502, 503, 504):
            log.warning("transient %s on %s (attempt %d)", resp.status_code, url, attempt)
            continue
        return resp
    log.error("gave up on %s", url)
    return None


def upsert_opening(eco: str | None, name: str | None, eco_url: str | None = None) -> Opening | None:
    """Find-or-create an Opening.

    Prefers matching by eco_url when available (chess.com always supplies one).
    Falls back to matching by (eco, name) — required for sources like Lichess
    that don't provide a URL, otherwise every game would mint a fresh duplicate
    Opening row for the same named opening.
    """
    if not (eco or name or eco_url):
        return None
    if eco_url:
        existing = Opening.query.filter_by(eco_url=eco_url).first()
        if existing:
            return existing
    elif eco or name:
        existing = Opening.query.filter_by(eco=eco, name=name).first()
        if existing:
            return existing
    opening = Opening(eco=eco, name=name, eco_url=eco_url)
    db.session.add(opening)
    db.session.flush()
    return opening
