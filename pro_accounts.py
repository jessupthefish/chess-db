"""Curated list of well-known chess.com accounts for the "Pro Games" feed.

Verified live against the chess.com API (real accounts, correct titles, follower
counts sanity-checked to rule out impostor/fan accounts) — see docs/ARCHITECTURE.md
for the verification notes. Synced via `flask sync-pros`, tagged "pro".
"""

PRO_ACCOUNTS = [
    {"source": "chesscom", "username": "magnuscarlsen"},   # Magnus Carlsen, GM
    {"source": "chesscom", "username": "hikaru"},           # Hikaru Nakamura, GM
    {"source": "chesscom", "username": "fabianocaruana"},   # Fabiano Caruana, GM
    {"source": "chesscom", "username": "gukeshdommaraju"},  # Gukesh D, GM (world champion)
    {"source": "chesscom", "username": "lachesisq"},        # Ian Nepomniachtchi, GM
    {"source": "chesscom", "username": "gmwso"},             # Wesley So, GM
    {"source": "chesscom", "username": "anishgiri"},         # Anish Giri, GM
    {"source": "chesscom", "username": "rpragchess"},        # Praggnanandhaa R, GM
    {"source": "chesscom", "username": "firouzja2003"},      # Alireza Firouzja, GM
    {"source": "chesscom", "username": "lovevae"},            # Wei Yi, GM
    {"source": "chesscom", "username": "gothamchess"},       # Levy Rozman, IM (top streamer)
    {"source": "chesscom", "username": "vincentkeymer"},     # Vincent Keymer, GM
    {"source": "chesscom", "username": "viditchess"},        # Vidit Gujrathi, GM
    {"source": "chesscom", "username": "lyonbeast"},         # Maxime Vachier-Lagrave, GM
    {"source": "chesscom", "username": "liemle"},             # Liem Le, GM
]

PRO_TAG_NAME = "pro"
