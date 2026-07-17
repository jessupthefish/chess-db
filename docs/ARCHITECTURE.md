# Architecture reference — dashboard, on-demand analysis, pro-games feed

Static reference for the expansion planned 2026-07-17. Written once, rarely changes. See `BUILD_LOG.md` for live status. Full plan reasoning lives in the approved plan file used to build this (not synced to this repo — this doc is the durable, repo-tracked summary a cold session should rely on).

## Deploy process (corrected 2026-07-17)

- Mac dev copy: `/Users/stevenjessup/Downloads/chess-db`
- Spaceship (Linux, CachyOS, 16 cores, no passwordless sudo): `nomad@spaceship.local:/home/nomad/chess-db`, systemd `--user` service `chess-db.service`, gunicorn `-w 4 -b 0.0.0.0:8000`, LAN-reachable at `http://10.0.0.106:8000`.
- **Deploy command**: `rsync -avz --exclude='.venv' --exclude='__pycache__' --exclude='.DS_Store' --exclude='.env' --exclude='chess.db*' --exclude='.bin' --exclude='.git' /Users/stevenjessup/Downloads/chess-db/ nomad@spaceship.local:~/chess-db/` then `ssh nomad@spaceship.local "systemctl --user restart chess-db"`. **Note the glob `chess.db*`, not just `chess.db`** — WAL mode (added Phase 0) generates `chess.db-shm`/`chess.db-wal` sidecar files that must never cross machines either (caught this live during the Phase 1 deploy: Mac's mismatched WAL sidecars got copied to spaceship; harmless in that instance only because SQLite's WAL salt-check safety mechanism ignored the mismatch, not something to rely on).
- **`chess.db` is NEVER pushed from Mac to spaceship.** Spaceship's copy is authoritative — it's the live, actively-used instance. If Mac-side testing needs real data, pull read-only: `rsync nomad@spaceship.local:~/chess-db/chess.db /tmp/test.db`, never push back.
- No Alembic. `db.create_all()` (called in `create_app()`) only **adds** missing tables — never `ALTER`s existing ones. Any future column addition needs a hand-written `ALTER TABLE`.
- No passwordless sudo on spaceship — all new software/services must be user-space (`~/chess-db/.bin/`) and systemd `--user` (not `/etc/systemd/system`, despite what the README's stale example shows).

## Schema additions

```python
class GameAnalysis(db.Model):
    __tablename__ = "game_analysis"
    game_id = db.Column(db.Integer, db.ForeignKey("game.game_id"), primary_key=True)
    engine = db.Column(db.String(50), default="stockfish")
    engine_options = db.Column(db.String(100))
    plies_done = db.Column(db.Integer, default=0)
    ply_total = db.Column(db.Integer)
    analyzed_at = db.Column(db.DateTime)   # NULL = in progress
    white_acpl = db.Column(db.Float)
    black_acpl = db.Column(db.Float)
    error = db.Column(db.String(500))
    game = db.relationship("Game", backref=db.backref("analysis", uselist=False))

class MoveEval(db.Model):
    __tablename__ = "move_eval"
    move_eval_id = db.Column(db.Integer, primary_key=True)
    game_id = db.Column(db.Integer, db.ForeignKey("game.game_id"), nullable=False, index=True)
    ply = db.Column(db.Integer, nullable=False)   # matches game_detail.html positions[] index
    score_cp = db.Column(db.Integer)              # white-POV; NULL if mate_in set
    mate_in = db.Column(db.Integer)
    best_move_uci = db.Column(db.String(10))
    best_move_san = db.Column(db.String(10))
    classification = db.Column(db.String(20))     # blunder/mistake/inaccuracy/None
    __table_args__ = (db.UniqueConstraint("game_id", "ply", name="uq_move_eval_game_ply"),)
```

No job queue — `analyzed_at IS NULL` is the sole "in progress" signal. Thresholds: >200cp = blunder, >100cp = mistake, >50cp = inaccuracy (vs. best line). Accuracy shown as ACPL + counts, NOT a literal chess.com-style percentage (that needs a win-probability curve transform, out of scope for v1).

`config.py`: `STOCKFISH_PATH = os.environ.get("STOCKFISH_PATH", "stockfish")`.

WAL mode: `PRAGMA journal_mode=WAL;` set via `event.listens_for(Engine, "connect")` near `db.init_app(app)` in `create_app()`. Not set before this change — required once a background thread commits per-ply progress concurrently with normal request traffic.

Startup self-heal (in `create_app()`): delete any `GameAnalysis` row with `analyzed_at IS NULL` (+ its `MoveEval` rows) on boot — recovers from a deploy restart killing an in-flight analysis thread.

## On-demand analysis routes

- `POST /games/<id>/analyze` — insert `GameAnalysis` row (PK collision on double-click → caught `IntegrityError` → respond `in_progress`, no app-level race handling needed), spawn background thread (one `chess.engine.SimpleEngine` per job, closed at the end), walk plies via `python-chess`, write `MoveEval` rows + increment `plies_done`, set `analyzed_at` when done. Returns 202 immediately.
- `GET /games/<id>/analysis` — poll: `not_started` / `in_progress` (+ `plies_done`/`ply_total`) / `done` (+ move list + ACPL) / `error`.
- Engine lifecycle: **spawn-per-use, not long-lived.** No shared engine handle across gunicorn's 4 worker processes.

## Live move-suggestion route

- `POST /api/analyze-position` — `{fen, game_id, ply}`. Checks `GameAnalysis`/`MoveEval` cache first if that game is fully analyzed; else spawns a short-lived engine, evaluates, returns `{score_cp, mate_in, best_move_san}` — never persisted.
- `game_detail.html` wiring: hooks into existing `goTo(idx)`. `AbortController` cancels in-flight requests on rapid navigation; stale-response guard discards replies for a `ply` no longer current; previous eval stays visible while a new one loads (no blank-flicker).

## Pro-games feed

**Curated personal accounts** (`pro_accounts.py`, repo root) — verified live against chess.com API 2026-07-17:
```python
PRO_ACCOUNTS = [
    {"source": "chesscom", "username": "magnuscarlsen"},
    {"source": "chesscom", "username": "hikaru"},
    {"source": "chesscom", "username": "fabianocaruana"},
    {"source": "chesscom", "username": "gukeshdommaraju"},
    {"source": "chesscom", "username": "lachesisq"},        # Ian Nepomniachtchi
    {"source": "chesscom", "username": "gmwso"},              # Wesley So
    {"source": "chesscom", "username": "anishgiri"},
    {"source": "chesscom", "username": "rpragchess"},         # Praggnanandhaa R
    {"source": "chesscom", "username": "firouzja2003"},
    {"source": "chesscom", "username": "lovevae"},             # Wei Yi
    {"source": "chesscom", "username": "gothamchess"},        # Levy Rozman, IM
    {"source": "chesscom", "username": "vincentkeymer"},
    {"source": "chesscom", "username": "viditchess"},         # Vidit Gujrathi
    {"source": "chesscom", "username": "lyonbeast"},          # Maxime Vachier-Lagrave
    {"source": "chesscom", "username": "liemle"},              # Liem Le
]
```
`flask sync-pros` CLI command calls existing `sync_user()` per account, tags each `Player` with a `"pro"` tag. `sync_user()` never sets `is_friend`, so pro accounts already don't appear on `/players`'s default self+friend view — no filter changes needed there.

**Deviation found during Phase 4 build**: `sync_user()` defaults to syncing a chess.com account's *entire* history — fine for the user's own account, but for prolific pros (Hikaru, gothamchess) that's tens of thousands of games, far more than "recent games" calls for. Added an optional `max_archives: int | None` param to `sync/chesscom.py:sync_user()` (backward compatible — personal syncs via the `/sync` page and `flask sync-chesscom` still default to full history) that limits to the N most recent monthly archives. `sync-pros` passes `max_archives=2`. Verified this keeps volume sane (Hikaru: 991 games/2 months, ~5s; full 15-account sync: ~20s, +2744 games total).

**Tournament broadcasts** (`sync/broadcasts.py`, curated list in `broadcast_tournaments.py`) — **built and verified working** (2026-07-17): 6 major tournaments spanning 2024-2026 (Candidates 2024, World Championship 2024, Tata Steel 2025+2026, Sinquefield Cup 2025, Norway Chess 2026), 345 games synced on both machines. New `PlayerIdentity.source = "fide"`, matched by FIDE ID (fallback: exact name match) — verified correctly deduplicates the same player across multiple games (14 World Championship games → exactly 2 distinct players).

**Real resolution methodology** (use this to add more tournaments — mechanical, not architecture work): `/api/broadcast/top` has no name-search and its `tier` field is too noisy alone (tier 4-5 includes youth/regional events, not just majors) — don't rely on it. Instead: (1) web-search `"<tournament name>" lichess.org/broadcast round`, grab any round-level URL's ID from results; (2) resolve to the parent tournament ID via `GET /api/broadcast/-/-/{roundId}` (slug segments can be `-`) → response's `tour.id`; (3) verify with `GET /api/broadcast/{tourId}.pgn` → must return HTTP `200` with real PGN (check the status code — a bare `curl` can return a 404 HTML page that's easy to mistake for content if you only eyeball the first few hundred chars). One call (`GET /api/broadcast/{tourId}.pgn`) exports **every round of the whole tournament as one PGN** — includes Lichess's own pre-computed `%eval`/`%clk` annotations for free.

**Known cosmetic quirk**: broadcast PGNs give "Last, First" (FIDE convention); `_format_name()` naively swaps to "First Last", which is wrong for Chinese/Korean/Vietnamese names where the surname conventionally stays first even in English (e.g. "Ding, Liren" → stored as "Liren Ding", not "Ding Liren"). Data linkage is unaffected (FIDE ID matching, not name matching), only display ordering — not fixed, low priority.

`Game.source = "broadcast"`, `Event` populated properly (first real use of that model beyond incidental creation) with name/location/dates from the tournament's own metadata.

**Unified feed**: `/pros` route + `templates/pro_games.html` — games where either player has the `"pro"` tag OR `Game.source == "broadcast"`, `ORDER BY played_at DESC`, paginated, reusing the `.game-table` markup pattern (same duplication convention as `games.html`/`player_detail.html`/`collection_detail.html`).

**Refresh**: systemd `--user` timers on spaceship, both `Type=oneshot` services invoking the respective `flask sync-*` CLI command via `EnvironmentFile=%h/chess-db/.env` (same pattern as `chess-db.service`). Both live: `chess-db-sync-pros.timer` (`OnCalendar=*-*-* 06:17:00`) and `chess-db-sync-broadcasts.timer` (`OnCalendar=*-*-* 06:23:00`), both `Persistent=true`, created + enabled 2026-07-17. Copy this working example rather than re-deriving:
```ini
# ~/.config/systemd/user/chess-db-sync-pros.service
[Unit]
Description=Chess DB — sync curated pro/streamer accounts
[Service]
Type=oneshot
WorkingDirectory=%h/chess-db
EnvironmentFile=%h/chess-db/.env
ExecStart=%h/chess-db/.venv/bin/flask sync-pros
```
```ini
# ~/.config/systemd/user/chess-db-sync-pros.timer
[Unit]
Description=Daily chess-db pro-account sync
[Timer]
OnCalendar=*-*-* 06:17:00
Persistent=true
[Install]
WantedBy=timers.target
```
Enable with `systemctl --user daemon-reload && systemctl --user enable --now chess-db-sync-pros.timer`.

## Stockfish

- Spaceship: no sudo → download official prebuilt Linux binary (check `/proc/cpuinfo` for bmi2/avx2) from Stockfish GitHub releases → `~/chess-db/.bin/stockfish`, `chmod +x`, smoke-test via `uci`/`uciok`. **`STOCKFISH_PATH=/home/nomad/chess-db/.bin/stockfish` must be added to `~/chess-db/.env`** on spaceship (already wired via `EnvironmentFile=` in `chess-db.service`) — without it, `config.py`'s default (`"stockfish"`, expecting `$PATH`) silently fails there. Confirmed installed both places and this env var is set (2026-07-17).
- Mac: `brew install stockfish` (on `$PATH`, no `STOCKFISH_PATH` override needed there).
