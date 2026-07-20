from datetime import datetime, timezone

from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()

# ── junction tables ──────────────────────────────────────────────────────

game_tag = db.Table(
    "game_tag",
    db.Column("game_id", db.Integer, db.ForeignKey("game.game_id"), primary_key=True),
    db.Column("tag_id", db.Integer, db.ForeignKey("tag.tag_id"), primary_key=True),
)

player_tag = db.Table(
    "player_tag",
    db.Column("player_id", db.Integer, db.ForeignKey("player.player_id"), primary_key=True),
    db.Column("tag_id", db.Integer, db.ForeignKey("tag.tag_id"), primary_key=True),
)

collection_game = db.Table(
    "collection_game",
    db.Column("collection_id", db.Integer, db.ForeignKey("collection.collection_id"), primary_key=True),
    db.Column("game_id", db.Integer, db.ForeignKey("game.game_id"), primary_key=True),
    db.Column("sort_order", db.Integer, default=0),
)

# ── entities ─────────────────────────────────────────────────────────────


class Player(db.Model):
    __tablename__ = "player"

    player_id = db.Column(db.Integer, primary_key=True)
    display_name = db.Column(db.String(120), nullable=False)
    is_self = db.Column(db.Boolean, default=False, index=True)
    is_friend = db.Column(db.Boolean, default=False)
    country = db.Column(db.String(10))
    title = db.Column(db.String(10))
    notes = db.Column(db.Text)

    identities = db.relationship("PlayerIdentity", backref="player", lazy="select")
    tags = db.relationship("Tag", secondary=player_tag, backref="tagged_players")

    def __repr__(self):
        label = self.title + " " if self.title else ""
        return f"<Player {label}{self.display_name}>"


class PlayerIdentity(db.Model):
    __tablename__ = "player_identity"

    identity_id = db.Column(db.Integer, primary_key=True)
    player_id = db.Column(db.Integer, db.ForeignKey("player.player_id"), nullable=False, index=True)
    source = db.Column(db.String(20), nullable=False)   # chesscom / lichess / uscf / fide
    username = db.Column(db.String(120), nullable=False)

    __table_args__ = (db.UniqueConstraint("source", "username", name="uq_identity"),)


class Opening(db.Model):
    __tablename__ = "opening"

    opening_id = db.Column(db.Integer, primary_key=True)
    eco = db.Column(db.String(5))
    name = db.Column(db.String(250))
    eco_url = db.Column(db.String(500))
    parent_opening_id = db.Column(db.Integer, db.ForeignKey("opening.opening_id"))

    parent = db.relationship("Opening", remote_side=[opening_id], backref="variations")


class Event(db.Model):
    __tablename__ = "event"

    event_id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(250), nullable=False)
    location = db.Column(db.String(250))
    format = db.Column(db.String(50))       # swiss / RR / arena / match
    start_date = db.Column(db.Date)
    end_date = db.Column(db.Date)
    source = db.Column(db.String(20))       # otb / chesscom / lichess


class Game(db.Model):
    __tablename__ = "game"

    game_id = db.Column(db.Integer, primary_key=True)
    source = db.Column(db.String(20), nullable=False)
    source_game_id = db.Column(db.String(200))
    source_url = db.Column(db.String(500))

    white_id = db.Column(db.Integer, db.ForeignKey("player.player_id"), nullable=False, index=True)
    black_id = db.Column(db.Integer, db.ForeignKey("player.player_id"), nullable=False, index=True)
    white_rating = db.Column(db.Integer)
    black_rating = db.Column(db.Integer)
    result = db.Column(db.String(10))
    termination = db.Column(db.String(50))
    rules = db.Column(db.String(20), default="chess")
    time_class = db.Column(db.String(20))
    time_control = db.Column(db.String(50))
    played_at = db.Column(db.DateTime, index=True)
    ply_count = db.Column(db.Integer)
    pgn = db.Column(db.Text, nullable=False)

    opening_id = db.Column(db.Integer, db.ForeignKey("opening.opening_id"), index=True)
    event_id = db.Column(db.Integer, db.ForeignKey("event.event_id"), index=True)
    notes = db.Column(db.Text)

    __table_args__ = (
        db.UniqueConstraint("source", "source_game_id", name="uq_game_source"),
    )

    white = db.relationship("Player", foreign_keys=[white_id], backref="games_as_white")
    black = db.relationship("Player", foreign_keys=[black_id], backref="games_as_black")
    opening = db.relationship("Opening", backref="games")
    event = db.relationship("Event", backref="games")
    tags = db.relationship("Tag", secondary=game_tag, backref="games")


class Tag(db.Model):
    __tablename__ = "tag"

    tag_id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), unique=True, nullable=False)
    color = db.Column(db.String(20), default="#6366f1")


class Collection(db.Model):
    __tablename__ = "collection"

    collection_id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    description = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    games = db.relationship("Game", secondary=collection_game, backref="collections")


# ── sync bookkeeping ─────────────────────────────────────────────────────

class ArchiveCache(db.Model):
    __tablename__ = "archive_cache"

    archive_url = db.Column(db.String(500), primary_key=True)
    etag = db.Column(db.String(200))
    last_modified = db.Column(db.String(200))
    fetched_at = db.Column(db.Integer)


# ── on-demand engine analysis ────────────────────────────────────────────
# No job queue — analysis is user-triggered per game, run in a background
# thread, not a batch worker pool. analyzed_at IS NULL is the sole
# "in progress" signal (see app.py's startup self-heal hook, which clears
# any row stuck NULL after an unclean restart).

class GameAnalysis(db.Model):
    __tablename__ = "game_analysis"

    game_id = db.Column(db.Integer, db.ForeignKey("game.game_id"), primary_key=True)
    engine = db.Column(db.String(50), default="stockfish")
    engine_options = db.Column(db.String(100))
    plies_done = db.Column(db.Integer, default=0)
    ply_total = db.Column(db.Integer)
    analyzed_at = db.Column(db.DateTime)
    white_acpl = db.Column(db.Float)
    black_acpl = db.Column(db.Float)
    error = db.Column(db.String(500))

    game = db.relationship("Game", backref=db.backref("analysis", uselist=False))


class MoveEval(db.Model):
    __tablename__ = "move_eval"

    move_eval_id = db.Column(db.Integer, primary_key=True)
    game_id = db.Column(db.Integer, db.ForeignKey("game.game_id"), nullable=False, index=True)
    ply = db.Column(db.Integer, nullable=False)
    score_cp = db.Column(db.Integer)
    mate_in = db.Column(db.Integer)
    best_move_uci = db.Column(db.String(10))
    best_move_san = db.Column(db.String(10))
    classification = db.Column(db.String(20))

    __table_args__ = (db.UniqueConstraint("game_id", "ply", name="uq_move_eval_game_ply"),)


class MoveEvalLine(db.Model):
    __tablename__ = "move_eval_line"

    move_eval_line_id = db.Column(db.Integer, primary_key=True)
    game_id = db.Column(db.Integer, db.ForeignKey("game.game_id"), nullable=False, index=True)
    ply = db.Column(db.Integer, nullable=False)       # positions[]-index (0..N) — NOT MoveEval.ply's move-number convention, see app.py
    rank = db.Column(db.Integer, nullable=False)      # 1 (best) .. 3
    score_cp = db.Column(db.Integer)
    mate_in = db.Column(db.Integer)
    best_move_uci = db.Column(db.String(10))
    best_move_san = db.Column(db.String(10))
    pv_san = db.Column(db.String(200))   # space-joined short preview, e.g. "e4 c6 d4 d5 Nc3"

    __table_args__ = (db.UniqueConstraint("game_id", "ply", "rank", name="uq_move_eval_line"),)


# ── opening explorer ─────────────────────────────────────────────────────
# Position-frequency tree built from stored PGN text (no engine involved —
# see opening_tree.py). Nodes are deduped by fen_key (board+turn+castling+ep,
# no move counters) so transpositions merge into one node regardless of move
# order. Capped at opening_tree.MAX_PLY plies; not a full-game move tree.

class OpeningNode(db.Model):
    __tablename__ = "opening_node"

    node_id = db.Column(db.Integer, primary_key=True)
    parent_id = db.Column(db.Integer, db.ForeignKey("opening_node.node_id"), index=True)
    ply = db.Column(db.Integer, nullable=False)
    fen_key = db.Column(db.String(90), nullable=False, unique=True, index=True)
    fen = db.Column(db.String(100), nullable=False)
    move_san = db.Column(db.String(10))
    move_uci = db.Column(db.String(10))
    opening_id = db.Column(db.Integer, db.ForeignKey("opening.opening_id"))

    parent = db.relationship("OpeningNode", remote_side=[node_id], backref="children")
    opening = db.relationship("Opening", backref="nodes")


class GamePosition(db.Model):
    __tablename__ = "game_position"

    game_id = db.Column(db.Integer, db.ForeignKey("game.game_id"), primary_key=True)
    ply = db.Column(db.Integer, primary_key=True)
    node_id = db.Column(db.Integer, db.ForeignKey("opening_node.node_id"), nullable=False, index=True)

    game = db.relationship("Game", backref="positions")
    node = db.relationship("OpeningNode", backref="game_positions")


# ── full-game position index ─────────────────────────────────────────────
# One 8-byte Zobrist hash per (game, ply) over the FULL game (unlike the
# opening tree, which caps at 40 plies) — powers /search/position. Built
# from stored PGN text by position_index.py; no engine involved. Hashes are
# chess.polyglot.zobrist_hash() values converted to signed 64-bit for SQLite.

class PositionHash(db.Model):
    __tablename__ = "position_hash"

    game_id = db.Column(db.Integer, db.ForeignKey("game.game_id"), primary_key=True)
    ply = db.Column(db.Integer, primary_key=True)   # positions[]-index: 0 = start position
    zobrist = db.Column(db.BigInteger, nullable=False, index=True)


class PuzzleAttempt(db.Model):
    """One attempt at a blunder puzzle. Puzzles aren't stored — they're derived
    live from MoveEval rows where classification='blunder' (see app.py's
    _puzzle_candidates); an attempt records (game_id, ply) in MoveEval's
    move-number ply convention plus what the user played."""
    __tablename__ = "puzzle_attempt"

    attempt_id = db.Column(db.Integer, primary_key=True)
    game_id = db.Column(db.Integer, db.ForeignKey("game.game_id"), nullable=False, index=True)
    ply = db.Column(db.Integer, nullable=False)   # MoveEval.ply convention (move number 1..N)
    move_uci = db.Column(db.String(10))
    correct = db.Column(db.Boolean, nullable=False)
    attempted_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
