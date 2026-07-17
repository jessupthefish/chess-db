# Chess DB

A personal chess game database and viewer. Syncs games from Chess.com and
Lichess, stores them in SQLite, and serves a Flask web app with an
interactive chessground board viewer.

## Features

- **Chess.com sync** with ETag-based incremental caching (only fetches new months)
- **Lichess sync** with incremental `since`-based caching (only fetches new games)
- **Game browser** with filtering by player, opening, time class, result, and tags
- **Interactive board viewer** (chessground + chess.js) with move list, keyboard nav
- **Tags and collections** for organizing your games
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

### systemd service

```ini
# /etc/systemd/system/chess-db.service
[Unit]
Description=Chess DB
After=network.target

[Service]
User=steven
WorkingDirectory=/path/to/chess-db
Environment="SECRET_KEY=generate-a-real-key-here"
ExecStart=/path/to/chess-db/.venv/bin/gunicorn -w 4 -b 127.0.0.1:8000 app:app
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now chess-db
```

## Environment variables

| Variable       | Default                | Description              |
|----------------|------------------------|--------------------------|
| `SECRET_KEY`   | `dev-key-change-me`    | Flask session secret     |
| `DATABASE_URL` | `sqlite:///chess.db`   | SQLAlchemy database URI  |

## Project structure

```
chess-db/
├── app.py              # Flask app, routes, CLI commands
├── config.py           # Configuration
├── models.py           # SQLAlchemy models (Player, Game, Opening, etc.)
├── sync/
│   ├── __init__.py
│   ├── common.py        # Shared HTTP retry + opening-upsert helpers
│   ├── chesscom.py     # Chess.com API sync module
│   └── lichess.py      # Lichess API sync module
├── templates/
│   ├── base.html       # Base layout with nav
│   ├── games.html      # Filterable game list
│   ├── game_detail.html # Board viewer + notes + tags
│   ├── players.html    # Player list
│   ├── player_detail.html
│   ├── sync.html       # Sync management page
│   ├── collections.html
│   └── collection_detail.html
├── static/
│   └── style.css       # Dark-mode-first responsive CSS
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
- [ ] Opening tree / repertoire view
- [ ] Game annotation (move-level comments)
- [ ] Cron-based auto-sync
