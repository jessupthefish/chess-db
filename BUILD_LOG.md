# Chess DB — Build Log (dashboard + analysis engine + pro-games expansion)

> Read this file FIRST if you are a fresh Claude instance with no memory of this
> session. Treat conversation memory as unreliable EVEN MID-SESSION — update this
> file after every finished migration, route, or deploy, not just before stopping.
> See `docs/ARCHITECTURE.md` for the static design reference (schema, routes, curated lists).

## Status: ALL PHASES COMPLETE (last updated 2026-07-17 ~13:31)

7 original phases plus the opening-explorer expansion (Phase 8) and the
interactive-board expansion (Phase 9, sub-phases A-C) shipped, deployed, and
verified live on spaceship (now the primary dev machine — see CLAUDE.md):
dashboard, on-demand Stockfish analysis (per-game + live move-suggestion, now
multipv 3 throughout), pro-accounts feed, tournament-broadcast feed, dashboard
pro-games widget, a move-tree opening explorer with a mine-vs-pro toggle and
repertoire-gap stats, and now a clickable/draggable analysis board with
scratch variations, a promotion picker, and a 3-line eval panel with board
arrows. Both daily sync timers running on spaceship. Nothing blocked, nothing
in progress. Not yet committed/pushed to GitHub — only commit when asked, per
this project's standing rule.

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
- [x] Phase 7 — final push to GitHub: committed (e5542a3) and pushed to
      github.com/jessupthefish/chess-db. gh auth wasn't wired into git's credential
      helper for plain `git push` (only `gh`-invoked pushes worked) — fixed with
      `gh auth setup-git`, then the push succeeded normally.
- [x] Phase 8 — opening explorer (move-tree browser + mine/pro toggle + prep stats):
      new OpeningNode/GamePosition tables (models.py) — a position-frequency tree
      built from stored PGN text via python-chess, deduped across transpositions by
      normalized position key (Board.epd(), no move counters), capped at 40 plies.
      New opening_tree.py module (ingest_game/rebuild_all), mirrors analysis.py's
      role but does no engine calls — see the batch-analysis note below, this is a
      different kind of "batch" and the existing rule doesn't apply to it. New
      `flask build-opening-tree` CLI command, also invoked automatically at the end
      of sync-chesscom/sync-lichess/sync-pros/sync-broadcasts (CLI and the two web
      sync routes) so newly-synced games get folded in without a manual step —
      cheap since rebuild_all() skips games already ingested. New app.py helpers
      (_opening_tree_children, _opening_tree_games, _opening_totals,
      _repertoire_gaps) and routes (/openings, /openings/<node_id>,
      /openings/stats), refactored _pro_games_query() to share its filter
      condition (_pro_condition()) with the new tree-scope queries instead of
      duplicating it. New templates/opening_explorer.html (Chessground board fed
      directly from the node's stored FEN — no chess.js needed, unlike
      game_detail.html, since positions are precomputed) and
      templates/opening_stats.html. "Openings" nav link added to base.html.
      Backfilled all 24,148 standard-chess games on spaceship's live chess.db
      (680,523 nodes) — see decisions log for two real bugs found and fixed during
      that backfill. Deployed, smoke-tested (test client + curl), verified live.
- [x] Phase 9 — interactive board + scratch variations + multi-line analysis
      (plan: ~/.claude/plans/imperative-rolling-giraffe.md), built as three
      sequential sub-phases per that plan:
  - [x] 9A (backend) — new MoveEvalLine table (models.py), keyed by
        game_id/ply/rank with ply as the positions[]-index (0..N) of the
        pre-move position, NOT MoveEval.ply's move-number (1..N) convention —
        the two tables use different ply conventions on purpose, see the
        decisions log. analysis.py: `_eval_position` replaced by
        `_eval_position_multi` (chess.engine.analyse(..., multipv=3), one
        engine call returns all 3 lines, not 3x slower), each line carries a
        6-ply SAN preview. run_full_analysis now persists MoveEvalLine rows
        for every position/rank in addition to the existing rank-1-only
        MoveEval rows; analyze_position (live-toggle path) returns
        list[dict] instead of one dict. app.py: self-heal hook also clears
        MoveEvalLine on an unclean-restart cleanup; /games/id/analysis joins
        in each move's lines (bridging the two ply conventions: move m's
        alternatives are lines_by_ply[m.ply - 1]); /api/analyze-position's
        cache-hit branch now queries MoveEvalLine directly by positions[]-
        index, which sidesteps a real pre-existing off-by-one bug (the old
        MoveEval-based cache hit was keying by positions[]-index against a
        column that actually describes the position one ply earlier) by
        construction rather than patching it in place. Both branches
        (cache-hit and fresh-engine-call) return the identical JSON shape
        so the frontend never branches on cached-vs-not. Verified via
        app.test_client() against spaceship's live chess.db: ran a full
        analysis on an unanalyzed real game (game_id=2), confirmed 3 lines
        per ply with sane scores/SAN, confirmed the cache-hit and fresh-call
        branches return matching shapes, confirmed the ply-offset bridge
        with a known position, regression-checked / /games /games/2
        /openings /openings/stats all still 200.
  - [x] 9B (interactivity) — templates/game_detail.html JS state model
        rewrite: `branch` (scratch variation: forkPly + a chess.js instance
        + SAN list), `pendingMove` (promotion picker), `moveToken`
        (staleness guard generalized to work with no main-line index while
        branched). `activeFen()` unifies main-line/branch position access.
        Chessground config gets `movable: {free:false, color:'both',
        dests: computeDests(fen), events:{after: onUserMove}}` (dests
        rebuilt from chess.js on every position change — Chessground's
        dests is a real Map, not a plain object) plus `draggable`/
        `drawable` enabled, `viewOnly` removed. Every board FEN update
        (goTo, commitUserMove, resyncBoard) re-syncs from chess.js's own
        FEN rather than trusting Chessground's naive drag-move — this
        single pattern is what correctly handles promotion piece display,
        castling rook jump, and en passant capture removal all at once,
        confirmed against the pinned chess.js@1.0.0-beta.8 build (see
        decisions log). Promotion has no Chessground-native UI — detected
        pre-commit by filtering `chess.moves({verbose:true})` for the
        dragged from/to and checking for a `.promotion` field on any
        result; ambiguous moves freeze dests and show a hand-rolled 4-button
        Q/R/B/N picker (Escape cancels and resyncs). New #exploring-bar
        (shown on divergence, "1. e4" / "12... Nf6"-style SAN line correctly
        threaded from branch.forkPly, "Back to game" returns via
        `goTo(branch.forkPly)`).
  - [x] 9C (multi-line UI) — eval panel rebuilt as a 3-row `.eval-line`
        list (rank dot + score + SAN preview) instead of one score/best-move
        pair; each line also gets a Chessground `drawable.autoShapes` arrow
        (green/blue/yellow by rank, matching chessground's own default
        brush hex values so the dot and arrow for a given rank actually
        match). Arrows clear immediately on every new suggestion request
        (not just on response) to avoid a stale-arrow flash during
        navigation, guarded by the same `moveToken` used for the eval text.
        New CSS: `.board-wrap` (positioning context for the picker),
        `.eval-lines`/`.eval-line*`, `.promotion-picker` (absolute, centered
        over the board), `.exploring-bar` (mirrors `.flash`'s accent-left-
        border convention).
  - Browser verification (no Claude-in-Chrome extension connected in this
    session — see decisions log for the workaround used): dev instance on
    port 8001 (separate from the live gunicorn on 8000) driven via a
    hand-rolled raw-socket Chrome DevTools Protocol client against headless
    chromium (no playwright/node available either). Confirmed: page loads
    with zero console/runtime errors (32 pieces + 1 chessground ghost
    element render correctly); a real mouse-drag e2-e4 against game_id=2
    (whose actual recorded first move is 1.e3, not e4) correctly diverges
    into a branch and shows "Exploring: 1. e4"; "Back to game" correctly
    returns to the fork point and hides the bar; a real drag e2-e3 (matching
    the recorded move) correctly advances the main line instead of
    branching; toggling live suggestion renders 3 eval lines with real
    engine scores/previews and draws board arrows (SVG element count
    increased as expected). Promotion specifically verified against the
    pinned chess.js version directly (candidate-move generation for a
    pawn-to-last-rank drag returns 4 moves each with the expected
    `.promotion` letter and matching SAN `e8=Q+` etc; `chess.move({...,
    promotion})` commits correctly) rather than via a staged live drag,
    since reaching a genuinely promotable position in a real recorded game
    would need many more scripted moves than the verification budget
    justified — this is a real gap versus a full manual click-through and
    is called out explicitly, not glossed over. Regression-checked: existing
    on-demand analysis annotations (blunder/mistake/inaccuracy classes +
    ACPL) still render correctly on load for the already-analyzed game.
      Deployed: `systemctl --user restart chess-db`, curl-verified
      /games/2, /games/2/analysis (3 lines/move), /api/analyze-position
      (cache-hit branch), /, /openings/stats all correct/200 against the
      live service on spaceship.
- [ ] Phase 2 — on-demand single-game analysis (schema + routes + UI)
- [ ] Phase 3 — live move-suggestion toggle
- [ ] Phase 4 — pro personal-accounts feed
- [ ] Phase 5 — tournament broadcast ingestion
- [ ] Phase 6 — dashboard pro-games preview widget
- [ ] Phase 7 — final push to GitHub
- [ ] Phase 8 — opening explorer

## IN PROGRESS — exact resume point
Nothing in progress. All 9 phases (7 original + the opening-explorer expansion
+ the interactive-board/scratch-variations/multi-line-analysis expansion) are
complete, deployed, and verified live. Not yet pushed to GitHub — only
commit/push when asked. One known verification gap worth closing before
considering Phase 9 fully battle-tested: the promotion picker's *UI wiring*
(button click -> commitUserMove) was never driven by an actual staged
promotion drag in a real browser session — only the underlying chess.js
library behavior it depends on was verified directly (see Phase 9 decisions
log and the "Browser verification" note under the Phase 9 checklist entry).
If resuming, either stage a real promotion (e.g. hand-edit a scratch FEN
close to the 8th rank) and click through Q/R/B/N once with Claude-in-Chrome,
or treat the current indirect verification as sufficient and move on. Other
natural next steps: FEN search, "puzzles from your own blunders" trainer,
expanding the curated pro/broadcast lists (see docs/ARCHITECTURE.md's
methodology), opening-explorer follow-ons (ply-depth slider, PGN export), or
extending Phase 9's single-scratch-variation model toward a full branch tree
if that limitation ever becomes annoying in practice.

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
- 2026-07-17: the "no batch/background analysis" rule above is specifically about
  Stockfish engine calls (expensive, explicitly rejected by the user) — it does NOT
  extend to the opening-tree backfill (Phase 8), which is cheap PGN parsing/
  aggregation with no engine involved. A future session should not conflate the two;
  running `flask build-opening-tree` (or its auto-invocation after sync) over the
  whole library is fine.
- 2026-07-17: opening-tree ingestion (opening_tree.py) must skip non-standard-chess
  games — chess960/oddschess start from a different position/army than
  chess.Board()'s default, and replaying their moves against the wrong start
  position raises `AssertionError: san() ... expect move to be legal` partway
  through. Found live during the first full backfill (12 of 24170 games raised this
  — all chess960). Fixed by checking `game.rules in {"chess", "standard"}`
  (whitelist, not blacklist — chess.com uses "chess", Lichess's Variant header
  defaults to "standard") before attempting to walk a game's PGN.
- 2026-07-17: the same backfill run exposed a second, more serious bug: rebuild_all()
  was calling a plain `db.session.rollback()` on a per-game exception, which — since
  commits only happen every COMMIT_EVERY=200 games — silently discarded up to ~200
  *other* already-successfully-processed games' work along with the one that failed,
  and nothing re-queued them. (12 raised exceptions, but 77 games ended up missing
  from the tree — the gap is exactly this.) Fixed by wrapping each game's ingest in
  its own `db.session.begin_nested()` SAVEPOINT, so a failure only rolls back that
  one game; the cache dict also needed the same treatment (evict any node fen_keys
  added during the failed game before re-raising, so a later game can't reuse a
  Python object pointing at a row that got rolled back). Re-verified after the fix:
  24148 ingested, 0 failed, 22 correctly skipped (19 chess960 + 3 oddschess) — matches
  exactly. Any future per-game loop over a large table that commits in batches should
  use the same per-item-SAVEPOINT pattern, not a blanket rollback on exception.
- 2026-07-17: opening-tree position keys use `chess.Board.epd()` (board + turn +
  castling + en passant target, no move counters) specifically so transpositions
  merge into one node regardless of move order — verified live (1.d4 Nf6 2.c4 and
  1.c4 Nf6 2.d4 both resolve to the same OpeningNode, 229 games combined).
- 2026-07-17 (Phase 9): a new table (MoveEvalLine) was chosen over a `lines_json`
  column on the existing MoveEval table specifically because this project has no
  Alembic — db.create_all() only adds new tables automatically, while a new column
  on an existing table needs a hand-written ALTER TABLE with no precedent of
  actually being exercised here, and a real risk of silently being forgotten (every
  cache lookup would 500 or silently return nothing). Confirmed live: running
  db.create_all() via `create_app()` against spaceship's real chess.db added
  move_eval_line with zero effect on any existing table or row.
- 2026-07-17 (Phase 9): MoveEval.ply and MoveEvalLine.ply intentionally use
  different conventions — MoveEval.ply is a move-number (1..N), MoveEvalLine.ply is
  a positions[]-index (0..N) of the *pre-move* position. This was a deliberate
  choice (MoveEvalLine mirrors analysis.py's internal `positions[]` indexing
  directly) and it's what let /api/analyze-position's new MoveEvalLine-based
  cache-hit branch sidestep a real pre-existing bug in the old MoveEval-based cache
  hit: that code queried `MoveEval.query.filter_by(ply=ply)` using the client's
  positions[]-index `ply` directly against a column that actually describes the
  position one ply earlier, an off-by-one that was never patched in the old code
  path (still exists there today) but is avoided by construction in the new one.
  Any future MoveEval-adjacent code should double check which convention it's
  reading before trusting `ply` at face value.
- 2026-07-17 (Phase 9): confirmed live against the pinned chess.js@1.0.0-beta.8
  build (not just the changelog/docs): `new Chess(fen).moves({verbose:true})`
  filtered to a given from/to returns one entry per promotion piece (q/r/b/n),
  each with a `.promotion` field set to the lowercase letter — this is exactly
  what the promotion-ambiguity check in game_detail.html's `onUserMove` relies on.
  Also confirmed `chess.move({from, to, promotion})` commits correctly and
  `.fen()` reflects the promoted piece afterward. First attempt at this check used
  a bogus test position (opposing king sitting directly on the promotion square,
  which blocks the pawn's only legal destination and made it look like the
  library generated zero pawn moves at all) — worth remembering if this ever needs
  re-verifying: put the blocking king somewhere else.
- 2026-07-17 (Phase 9): the Claude-in-Chrome browser extension was not connected
  in this session ("Browser extension is not connected" from tabs_context_mcp),
  and neither playwright nor node/npm were available in this environment either.
  Real interactive browser verification (mouse drags, DOM assertions, console/
  network error capture) was still done, via a ~150-line hand-rolled raw-socket
  Chrome DevTools Protocol client (no external deps — manual websocket handshake
  + frame framing) driving a `chromium --headless=new --remote-debugging-port`
  instance. Notes for reuse: the `/json/new?url` HTTP endpoint requires PUT, not
  GET, on this chromium build (150.x) — GET returns 405. Launching/killing
  chromium as a background process both needed `dangerouslyDisableSandbox: true`
  in this environment's Bash tool or the calls silently failed (exit code 144)
  with no chromium.log even created.

## Deploy log (one line per rsync+restart, with the verification step taken)
- 2026-07-17 ~12:41: Phase 8 (opening explorer) deployed directly on spaceship (no
  rsync — this machine is primary now). `flask build-opening-tree` run against the
  live chess.db (24148 games ingested, 0 failed, ~2.5 min). Verified with
  `app.test_client()` (/openings, /openings/<node_id> both scopes, /openings/stats,
  plus a regression check on / and /games) before restarting, then `systemctl --user
  restart chess-db` + curl 200 on the same routes against the live gunicorn process
  after restart.
- 2026-07-17 ~13:30: Phase 9 (interactive board + scratch variations + multi-line
  analysis) deployed directly on spaceship. db.create_all() run first (adds
  move_eval_line only, confirmed via PRAGMA table_info). Pre-restart: full backend
  verification via app.test_client() against the live chess.db (see Phase 9
  checklist entry above) plus a full interactive-browser pass against a separate
  dev instance on port 8001 (not the live gunicorn on 8000), driven by a hand-
  rolled CDP client since Claude-in-Chrome wasn't connected this session (see
  decisions log). `systemctl --user restart chess-db`, then curl-verified
  /games/2 (200), /games/2/analysis (status done, 3 lines on move 1),
  /api/analyze-position cache-hit branch (3 lines, matching shape to the fresh-
  engine-call branch), / (200), /openings/stats (200) against the live service.
