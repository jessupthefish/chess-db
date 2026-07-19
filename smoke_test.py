"""Deploy smoke harness — run before and after every deploy, on both machines.

Usage: .venv/bin/python smoke_test.py
Hits every route with the Flask test client against the local chess.db and
asserts status codes. Extend the ROUTES list as new phases add routes.
Exits non-zero on any failure so it can gate a deploy.
"""

import sys
import time

from app import create_app
from models import Game, db


def main() -> int:
    app = create_app()
    client = app.test_client()

    with app.app_context():
        game = Game.query.order_by(Game.game_id).first()
        game_id = game.game_id if game else None
        self_exists = (
            db.session.execute(db.text("SELECT 1 FROM player WHERE is_self = 1 LIMIT 1")).first()
            is not None
        )

    # (path, allowed status codes) — 302 allowed where a missing self player redirects
    routes = [
        ("/", (200, 302)),
        ("/games", (200,)),
        ("/players", (200,)),
        ("/pros", (200,)),
        ("/collections", (200,)),
        ("/sync", (200,)),
        ("/openings", (200, 302)),
        ("/openings/stats", (200, 302)),
        ("/stats", (200, 302)),
        ("/stats?time_class=blitz", (200, 302)),
    ]
    if game_id is not None:
        routes.append((f"/games/{game_id}", (200,)))

    failures = []
    for path, allowed in routes:
        t0 = time.monotonic()
        resp = client.get(path)
        ms = (time.monotonic() - t0) * 1000
        ok = resp.status_code in allowed
        print(f"{'OK ' if ok else 'FAIL'} {resp.status_code} {path} ({ms:.0f} ms)")
        if not ok:
            failures.append((path, resp.status_code))

    if not self_exists:
        print("note: no is_self player in this DB — self-scoped pages exercised via redirect only")

    if failures:
        print(f"\n{len(failures)} failure(s): {failures}")
        return 1
    print(f"\nall {len(routes)} routes passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
