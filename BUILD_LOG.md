# Chess DB — Build Log (dashboard + analysis engine + pro-games expansion)

> Read this file FIRST if you are a fresh Claude instance with no memory of this
> session. Treat conversation memory as unreliable EVEN MID-SESSION — update this
> file after every finished migration, route, or deploy, not just before stopping.
> See `docs/ARCHITECTURE.md` for the static design reference (schema, routes, curated lists).

## Status: Phase 0 — foundations in progress (last updated 2026-07-17 ~02:00)

## BLOCKED — needs user input
(none currently)

## Environment facts (rarely change — read once)
- Mac dev: /Users/stevenjessup/Downloads/chess-db
- Spaceship: nomad@spaceship.local:/home/nomad/chess-db, systemd --user,
  gunicorn 0.0.0.0:8000, NO passwordless sudo available
- Deploy: rsync (exclude .venv __pycache__ .env chess.db .bin .git) then
  `ssh nomad@spaceship.local "systemctl --user restart chess-db"`
- chess.db authority: SPACESHIP'S COPY IS AUTHORITATIVE. Never rsync chess.db
  from Mac to spaceship. Never overwrite spaceship's chess.db.
- DB: SQLite, no Alembic — db.create_all() only ADDS tables, never ALTERs existing ones
- Stockfish: not yet installed on either machine (Phase 0, in progress)
- self player_id = 18240 (nomadchessty), friends = jaimemarron (player_id=1, ~19.5k games),
  naye36/Nayely Marron (player_id=18747, ~78 games)
- GitHub repo: github.com/jessupthefish/chess-db (public), origin remote set, gh CLI authenticated
- Overnight execution: caffeinate -dis running (Mac won't idle-sleep), CronCreate fallback
  routine setup is part of Phase 0 (not yet done as of this writing)

## Phase checklist
- [x] Phase 0a — git init + baseline commit + GitHub push (done in prior session segment)
- [x] Phase 0b — docs/ARCHITECTURE.md, BUILD_LOG.md
- [x] Phase 0c — Stockfish install on Mac (brew, /opt/homebrew/bin/stockfish) + spaceship
      (bmi2 build, ~/chess-db/.bin/stockfish) — both smoke-tested with uci/uciok
- [x] Phase 0d — WAL mode PRAGMA (app.py, event.listens_for(Engine, "connect")), verified
      via PRAGMA journal_mode -> wal, regression-tested /games /players /sync all 200
- [x] Phase 0e — overnight fallback: SKIPPED by user decision after discovering both
      CronCreate (session-only, dies with this session) and cloud routines (no access
      to spaceship/local files/LAN — can't deploy or test) can't actually provide the
      "survives a full session death" safety net originally intended. Relying on
      /loop + caffeinate only. If this session dies outright, a human needs to start a
      fresh session and point it at this file.
- [x] Phase 1 — dashboard: _self_player/_recent_games/_rating_trend/_sparkline_svg helpers
      added to app.py; / route replaced redirect with real dashboard; templates/dashboard.html
      + templates/_game_row.html partial (also used by games.html now); dataviz skill invoked
      before writing sparkline SVG code (small multiples per time_class, 2px line, direct
      end-label, no gridlines/legend per stat-card sparkline guidance). Deployed to spaceship
      and verified live (curl 200 on / and /games, WAL mode confirmed active on spaceship's
      real chess.db, game count still 21081 post-deploy).
- [x] Phase 2 — on-demand single-game analysis: GameAnalysis/MoveEval models added,
      analysis.py (run_full_analysis + analyze_position, cp_loss classification with
      standard 200/100/50 thresholds), POST /games/id/analyze + GET /games/id/analysis
      routes, startup self-heal hook, game_detail.html Analyze button + poll loop +
      move-list blunder/mistake/inaccuracy CSS annotations + ACPL display. Tested
      end-to-end locally (game 170, 10 plies, sane classification/ACPL output),
      double-click race handling verified, self-heal verified against a simulated
      stuck row. Deployed to spaceship, schema migrated over SSH, STOCKFISH_PATH gotcha
      found and fixed (see decisions log), verified live end-to-end on spaceship too.
- [x] Phase 3 — live move-suggestion toggle: POST /api/analyze-position route (cache-
      aware — checks GameAnalysis/MoveEval first, falls through to analysis.
      analyze_position() spawn-and-close), reused analysis.py's engine-invocation code
      rather than duplicating it. game_detail.html: checkbox toggle + eval panel wired
      into the existing goTo(idx), AbortController + stale-response guard (double-checked
      after the await too, not just before), previous eval stays visible while loading
      (no blank-flicker). Fixed a real gap found during testing: invalid FEN input was
      crashing with a raw 500 instead of a clean 400 — now caught. JS syntax verified via
      `node --check` on the extracted module script. Deployed to spaceship, verified live
      (both the uncached engine-query path and page load).
- [x] Phase 4 — pro personal-accounts feed: pro_accounts.py (15 verified chess.com
      accounts), flask sync-pros CLI command tagging "pro", /pros route + pro_games.html
      template, "Pros" nav link. IMPORTANT deviation from the original plan, found during
      testing: sync_user() defaults to FULL account history — for prolific accounts
      (Hikaru, gothamchess) that's tens of thousands of games, wildly more than "recent
      games" calls for. Added max_archives param to sync/chesscom.py's sync_user()
      (backward compatible, personal-account syncs still default to full history) and
      pass max_archives=2 for pro accounts — verified this keeps volumes sane (Hikaru:
      991 games in 2 months, ~5s; full 15-account sync: ~20s total, +2744 games). All 15
      confirmed tagged "pro", confirmed NOT appearing on default /players view (no filter
      change needed — sync_user() never sets is_friend). Deployed to spaceship, sync-pros
      run live there too, daily systemd --user timer (chess-db-sync-pros.timer, 06:17
      daily) created and enabled, verified /pros returns 200 with real data on both.
- [x] Phase 5 — tournament broadcast ingestion: sync/broadcasts.py + broadcast_tournaments.py
      (6 verified major tournaments 2024-2026: Candidates 2024, World Championship 2024,
      Tata Steel 2025+2026, Sinquefield Cup 2025, Norway Chess 2026). KEY DISCOVERY: the
      whole-tournament PGN export endpoint (GET /api/broadcast/{tourId}.pgn, exports ALL
      rounds in one call, includes Lichess's own %eval/%clk annotations) works great, but
      finding tournament IDs required a real methodology (web-search -> round ID -> resolve
      via GET /api/broadcast/-/-/{roundId} -> verify .pgn returns 200) since /api/broadcast/top
      has no name-search and tier alone is too noisy — full methodology documented in
      docs/ARCHITECTURE.md for expanding the list later (mechanical, not architecture work).
      New PlayerIdentity source="fide" identity path, verified correctly dedupes (14 WC games
      -> exactly 2 distinct players). flask sync-broadcasts CLI command, /pros route already
      had the OR source=="broadcast" condition from Phase 4 so no route changes needed —
      broadcast games appeared in the feed automatically. Known minor cosmetic quirk: "Last,
      First" -> "First Last" reformatting is wrong for Chinese/Korean/Vietnamese names (e.g.
      "Ding, Liren" displays as "Liren Ding") — data linkage unaffected, not fixed, low
      priority. 345 games synced and verified on both Mac and spaceship, second daily systemd
      timer (chess-db-sync-broadcasts.timer, 06:23 daily) created and enabled on spaceship.
- [x] Phase 6 — dashboard pro-games preview widget: factored a shared _pro_games_query()
      helper (app.py) out of the duplicated filter logic that had crept into both
      dashboard() and pro_games() — same union condition (pro tag OR source=="broadcast")
      used by both now, no drift risk. Dashboard shows 5 most recent pro/broadcast games
      + "See all" link to /pros, plus a "Watch pro games" quick-action link. Deployed,
      verified live.
- [ ] Phase 7 — final push to GitHub  <- NEXT
- [ ] Phase 2 — on-demand single-game analysis (schema + routes + UI)
- [ ] Phase 3 — live move-suggestion toggle
- [ ] Phase 4 — pro personal-accounts feed
- [ ] Phase 5 — tournament broadcast ingestion
- [ ] Phase 6 — dashboard pro-games preview widget
- [ ] Phase 7 — final push to GitHub

## IN PROGRESS — exact resume point
Phase 5 (tournament broadcast ingestion) is complete and deployed, verified live on both
machines, both daily timers running. Starting Phase 6 (dashboard pro-games preview
widget): add a small "recent pro games" widget to templates/dashboard.html, querying the
same union condition /pros uses (pro tag OR source=="broadcast"), limited to ~5 games,
linking to /pros for the full feed. Reuse _game_row.html partial. Should be a quick,
low-risk addition — no new routes needed, just a new query in the dashboard() route
(app.py) and a new section in dashboard.html. After this, Phase 7 (final git push) is
the last remaining phase.

## Decisions & gotchas (append-only, never delete, append the moment discovered)
- 2026-07-17: db.create_all() doesn't ALTER existing tables — future column additions
  need a hand-written ALTER TABLE step.
- 2026-07-17: rsync deploy command did NOT exclude chess.db originally — fixed. Always
  double check the exclude list before any spaceship rsync.
- 2026-07-17: No batch/background pre-analysis of the whole game library — explicitly
  rejected by user. Analysis is on-demand only (per-game "Analyze" button, or live
  "move suggestion" toggle). Do not build a job-queue/worker-pool architecture.
- 2026-07-17: Lichess broadcast API has no reliable tournament name-search — curated
  tournament list must be hand-resolved (see docs/ARCHITECTURE.md), not auto-discovered
  via pagination.
- 2026-07-17: chess.com API redirects usernames to lowercase (301) — curl/requests calls
  must follow redirects or use lowercase directly.
- 2026-07-17: spaceship has no passwordless sudo — confirmed twice now (ufw firewall rule
  earlier, and again when considering pacman for Stockfish). All new spaceship software
  must be user-space; all new services must be systemd --user, never /etc/systemd/system.
- 2026-07-17: rsync exclude list must be `chess.db*` (glob), not just `chess.db` — WAL mode
  generates chess.db-shm/chess.db-wal sidecars that must never cross machines either. Caught
  live during the Phase 1 deploy; harmless that time only because SQLite's WAL salt-check
  safety mechanism ignored the mismatched sidecar files, not something to rely on. Fixed in
  docs/ARCHITECTURE.md's documented deploy command and .gitignore.
- 2026-07-17: STOCKFISH_PATH must be explicitly set in spaceship's ~/chess-db/.env
  (EnvironmentFile= already wired into chess-db.service) — the binary lives at
  ~/chess-db/.bin/stockfish there, not on $PATH like it is via Homebrew on the Mac. Without
  this, config.py's default ("stockfish") would silently fail to find the binary on every
  analysis attempt on spaceship. Fixed and verified live (POST /games/170/analyze on
  spaceship completed successfully with real ACPL/classification data).

## Deploy log (one line per rsync+restart, with the verification step taken)
(none yet this expansion — first deploy will be after Phase 1 dashboard lands)
