# Chess DB

A personal chess game database and viewer. Syncs games from Chess.com and
Lichess, stores them in SQLite, and serves a Flask web app with a dashboard,
an interactive chessground board viewer, on-demand Stockfish analysis, and a
feed of games from top pros and major tournament broadcasts.

## Features

- **Dashboard** — your record, rating trend (sparklines), top openings, recent games
- **Chess.com sync** with ETag-based incremental caching (only fetches new months)
- **Lichess sync** with incremental `since`-based caching (only fetches new games)
- **On-demand Stockfish analysis** — click "Analyze" on any game for a full
  engine pass (blunder/mistake/inaccuracy annotations, ACPL), or toggle live
  move suggestions while browsing any board. Strictly on-demand, never a
  background bulk pre-analysis of your whole library.
- **Pro games feed** (`/pros`) — recent games from a curated list of top
  titled players' chess.com accounts, plus major tournament broadcasts
  (Candidates, World Championship, Tata Steel, Norway Chess, Grand Chess
  Tour) pulled from Lichess. Auto-refreshes daily via systemd timers.
- **Game browser** with filtering by player, opening, time class, result, source, and tags
- **Interactive board viewer** (chessground + chess.js) with move list, keyboard nav
- **Tags and collections** for organizing your games and players
- **Personal notes** on any game
- **Player identity linking** — multiple online accounts map to one person
- **CLI + web UI** for syncing and administration

## Quick start

```bash
# Clone / copy the project
cd chess-db

# Create a virtualenv
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Install Stockfish (needed for on-demand analysis)
brew install stockfish              # macOS
# or download a prebuilt binary from https://github.com/official-stockfish/Stockfish/releases
# and set STOCKFISH_PATH to point at it if it's not on $PATH

# Initialize the database
flask init-db

# Sync your games
flask sync-chesscom YOUR_USERNAME
flask sync-lichess YOUR_LICHESS_USERNAME

# Mark yourself (--source defaults to chesscom)
flask mark-self YOUR_USERNAME
flask mark-self YOUR_LICHESS_USERNAME --source lichess

# Mark friends (optional)
flask mark-friend FRIEND_USERNAME

# Link a second account (e.g. Lichess) to a player who already exists
flask link-identity PLAYER_ID lichess THEIR_LICHESS_USERNAME

# Optional: seed the pro games feed
flask sync-pros
flask sync-broadcasts

# Run the dev server
flask run --debug
```

Open http://localhost:5000 and you're set.

## Deployment (home server / VPS)

For production behind nginx or caddy:

```bash
# Install gunicorn (already in requirements.txt)
gunicorn -w 4 -b 127.0.0.1:8000 app:app
```

### Caddy (simplest — auto-HTTPS)

```
chess.stevenjessup.com {
    reverse_proxy localhost:8000
}
```

### nginx

```nginx
server {
    listen 80;
    server_name chess.stevenjessup.com;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location /static/ {
        alias /path/to/chess-db/static/;
        expires 30d;
    }
}
```

Add certbot / Let's Encrypt for HTTPS.

### systemd — user service (no root required)

If you don't have (or don't want to use) root on the host, a `systemd --user`
service works just as well and survives reboot via `loginctl enable-linger`:

```ini
# ~/.config/systemd/user/chess-db.service
[Unit]
Description=Chess DB
After=network.target

[Service]
WorkingDirectory=%h/chess-db
EnvironmentFile=%h/chess-db/.env
ExecStart=%h/chess-db/.venv/bin/gunicorn -w 4 -b 0.0.0.0:8000 app:app
Restart=always

[Install]
WantedBy=default.target
```

```bash
loginctl enable-linger $USER   # once, so the service survives logout — needs sudo
echo "SECRET_KEY=$(python3 -c 'import secrets; print(secrets.token_hex(32))')" > ~/chess-db/.env
echo "STOCKFISH_PATH=/path/to/stockfish" >> ~/chess-db/.env   # if not on $PATH
systemctl --user daemon-reload
systemctl --user enable --now chess-db
```

Pro/broadcast feed auto-refresh (optional), same `--user` pattern — a
`Type=oneshot` service + a `.timer` unit per sync job:

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

```bash
systemctl --user enable --now chess-db-sync-pros.timer
```

Mirror the same two files (swap `sync-pros` for `sync-broadcasts`) for the tournament feed.

### traditional root systemd service

If you do have root, the standard `/etc/systemd/system/chess-db.service` +
`sudo systemctl enable --now chess-db` pattern works the same way — just drop
the `%h`/`EnvironmentFile` bits for a `User=` directive and inline `Environment=` lines instead.

## Environment variables

| Variable         | Default                | Description                                   |
|-------------------|------------------------|------------------------------------------------|
| `SECRET_KEY`      | `dev-key-change-me`    | Flask session secret                            |
| `DATABASE_URL`    | `sqlite:///chess.db`   | SQLAlchemy database URI                         |
| `STOCKFISH_PATH`  | `stockfish`            | Path to the Stockfish binary (defaults to `$PATH`) |

## Project structure

```
chess-db/
├── app.py                     # Flask app, routes, CLI commands
├── config.py                  # Configuration
├── models.py                  # SQLAlchemy models (Player, Game, Opening, GameAnalysis, etc.)
├── analysis.py                # On-demand Stockfish analysis (single game + single position)
├── pro_accounts.py            # Curated chess.com accounts for the pro-games feed
├── broadcast_tournaments.py   # Curated Lichess broadcast tournament IDs
├── sync/
│   ├── __init__.py
│   ├── common.py               # Shared HTTP retry + opening-upsert helpers
│   ├── chesscom.py            # Chess.com API sync module
│   ├── lichess.py             # Lichess API sync module
│   └── broadcasts.py          # Lichess tournament-broadcast sync module
├── templates/
│   ├── base.html              # Base layout with nav
│   ├── dashboard.html         # Homepage — record, rating trend, top openings, recent games
│   ├── games.html             # Filterable game list
│   ├── game_detail.html       # Board viewer + on-demand analysis + notes + tags
│   ├── players.html           # Player list
│   ├── player_detail.html
│   ├── player_openings.html
│   ├── pro_games.html         # Pro/tournament games feed
│   ├── sync.html              # Sync management page
│   ├── collections.html
│   ├── collection_detail.html
│   └── _game_row.html         # Shared game-table row partial
├── static/
│   └── style.css              # Dark-mode-first responsive CSS
├── docs/
│   └── ARCHITECTURE.md        # Design reference for the analysis/dashboard/pro-games system
├── requirements.txt
└── README.md
```

## Keyboard shortcuts (game viewer)

| Key     | Action         |
|---------|----------------|
| ←       | Previous move  |
| →       | Next move      |
| Home    | Go to start    |
| End     | Go to end      |

## Roadmap

- [ ] Manual PGN input for OTB games
- [ ] Opening repertoire explorer (move-sequence tree with win rate per branch)
- [ ] Position/FEN search across your library
- [ ] "Puzzles from your own games" trainer, built from analyzed blunders
- [ ] Expand the curated pro/broadcast tournament lists (see `docs/ARCHITECTURE.md`
      for the tournament-ID resolution methodology)
