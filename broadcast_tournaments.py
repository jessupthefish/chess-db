"""Curated list of major chess tournament broadcasts on Lichess.

Each entry is a verified Lichess broadcast tournament ID (confirmed live via
GET /api/broadcast/{id} and /api/broadcast/{id}.pgn this session — see
docs/ARCHITECTURE.md for the resolution methodology). Lichess's broadcast API
has no reliable name-search, so this list is hand-curated rather than
auto-discovered; adding more tournaments is a mechanical follow-up (search
"<tournament name> lichess broadcast", resolve a round ID to its parent
tournament ID via GET /api/broadcast/-/-/{roundId}, verify the .pgn export
returns 200), not an architecture change.
"""

BROADCAST_TOURNAMENTS = [
    {"id": "wEuVhT9c", "name": "FIDE Candidates 2024 | Open"},
    {"id": "n3yHJI5Y", "name": "FIDE World Championship 2024"},
    {"id": "jR0BiOwR", "name": "Tata Steel Chess 2025 | Masters"},
    {"id": "09ZG4Ez5", "name": "GCT: Sinquefield Cup 2025"},
    {"id": "3COxSfdj", "name": "Tata Steel Chess 2026 | Masters"},
    {"id": "kDQUxYbE", "name": "Norway Chess 2026 | Open"},
]
