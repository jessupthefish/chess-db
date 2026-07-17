# Chess DB — project context

Personal chess game database and viewer (Flask + SQLAlchemy + SQLite). Read
`BUILD_LOG.md` and `docs/ARCHITECTURE.md` first — they're the durable, kept-
current references for this project's design and history. This file is just
the orientation layer on top of them.

## Where things stand (as of 2026-07-17)

Development has moved **onto this machine (spaceship)** as the primary
location — it's also where the app is deployed and where `chess.db` (the
real, authoritative data) lives. A Mac dev copy exists at
`/Users/stevenjessup/Downloads/chess-db` but is now secondary; don't assume
it's kept in sync going forward unless told otherwise.

- **Deployed service**: `chess-db.service`, a systemd `--user` unit (no root
  on this machine — everything is user-space, see below). Gunicorn on
  `0.0.0.0:8000`, reachable at `http://10.0.0.106:8000` / `http://spaceship.local:8000`
  from any device on the home LAN.
- **After editing code here**: `systemctl --user restart chess-db` picks it
  up. No more rsync-from-Mac deploy step needed now that this *is* the
  primary copy — just edit, test, restart, commit, push.
- **Database**: `chess.db` in this directory, WAL mode, **not tracked in
  git** (`.gitignore` covers `chess.db*`). It's the live production data —
  treat it with the same care you'd give any real user's data store.
- **No passwordless sudo on this machine.** Anything new needs to be
  user-space (binaries in `~/chess-db/.bin/` or `~/bin/`, services as
  `systemd --user`, never `/etc/systemd/system`). This has bitten past work
  twice already (a ufw firewall rule, Stockfish install) — see
  `docs/ARCHITECTURE.md` and `BUILD_LOG.md`'s decisions log for the details.
- **Two daily systemd `--user` timers** already running: `chess-db-sync-pros.timer`
  and `chess-db-sync-broadcasts.timer` (06:17 / 06:23 daily) — keep the pro-games
  feed fresh automatically, no action needed unless something breaks.

## Tooling on this machine

- **Stockfish**: `~/chess-db/.bin/stockfish` (not on `$PATH` — `STOCKFISH_PATH`
  is set in `~/chess-db/.env`, which the systemd unit loads via `EnvironmentFile=`).
- **git**: fully set up, `origin` → `github.com/jessupthefish/chess-db`,
  `master` tracks `origin/master`.
- **gh (GitHub CLI)**: installed at `~/bin/gh` (**not on `$PATH`** — call it
  by full path, or add `~/bin` to `PATH` yourself if you want; that wasn't
  done automatically since it's a persistent shell-profile change nobody
  explicitly asked for). Authenticated as `jessupthefish`, `gh auth setup-git`
  already run so plain `git push`/`git pull` work without needing the `gh`
  binary in the loop.

## Conventions

- Deploy verification pattern used throughout this project: after any change,
  smoke-test routes with a Flask test client (`app.test_client()`) before
  and after restarting the live service — see `BUILD_LOG.md`'s deploy log
  for the exact style used.
- Only commit/push when actually asked to — this project's owner has been
  explicit about that boundary in past sessions.
- Analysis is strictly on-demand (per-game "Analyze" button, live move-suggestion
  toggle) — **never** build automatic bulk/background analysis of the whole
  game library. This was an explicit, deliberate decision, not an oversight.
