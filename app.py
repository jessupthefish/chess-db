"""
app.py — Flask application for the chess game database.
Run:  flask run --debug
CLI:  flask sync-chesscom <username>
      flask init-db
      flask mark-self <chesscom_username>
"""

import threading
from datetime import datetime, timezone

import click
from flask import Flask, abort, flash, jsonify, redirect, render_template, request, url_for
from sqlalchemy import and_, case, event, func
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from config import Config
from models import (
    Collection,
    Event,
    Game,
    GameAnalysis,
    MoveEval,
    Opening,
    Player,
    PlayerIdentity,
    Tag,
    collection_game,
    db,
    game_tag,
)


def _player_record(player_id, opponent_id=None):
    """Aggregate win/loss/draw record for a player, optionally restricted to games
    against one specific opponent (head-to-head). Pure SQL aggregation — avoids
    loading full Game rows (incl. the pgn text column) just to tally results."""
    filters = [(Game.white_id == player_id) | (Game.black_id == player_id)]
    if opponent_id is not None:
        filters.append((Game.white_id == opponent_id) | (Game.black_id == opponent_id))

    win_case = case(
        (and_(Game.white_id == player_id, Game.result == "1-0"), 1),
        (and_(Game.black_id == player_id, Game.result == "0-1"), 1),
        else_=0,
    )
    loss_case = case(
        (and_(Game.white_id == player_id, Game.result == "0-1"), 1),
        (and_(Game.black_id == player_id, Game.result == "1-0"), 1),
        else_=0,
    )
    draw_case = case((Game.result == "1/2-1/2", 1), else_=0)

    wins, losses, draws, total = (
        db.session.query(
            func.coalesce(func.sum(win_case), 0),
            func.coalesce(func.sum(loss_case), 0),
            func.coalesce(func.sum(draw_case), 0),
            func.count(),
        )
        .filter(*filters)
        .one()
    )
    return {"wins": wins, "losses": losses, "draws": draws, "total": total}


def _opening_breakdown(player_id, limit=None):
    """Per-opening win/loss/draw breakdown for a player, most-played first."""
    win_case = case(
        (and_(Game.white_id == player_id, Game.result == "1-0"), 1),
        (and_(Game.black_id == player_id, Game.result == "0-1"), 1),
        else_=0,
    )
    loss_case = case(
        (and_(Game.white_id == player_id, Game.result == "0-1"), 1),
        (and_(Game.black_id == player_id, Game.result == "1-0"), 1),
        else_=0,
    )
    draw_case = case((Game.result == "1/2-1/2", 1), else_=0)

    q = (
        db.session.query(
            Opening.opening_id,
            Opening.eco,
            Opening.name,
            func.count(Game.game_id).label("total"),
            func.coalesce(func.sum(win_case), 0).label("wins"),
            func.coalesce(func.sum(loss_case), 0).label("losses"),
            func.coalesce(func.sum(draw_case), 0).label("draws"),
        )
        .join(Game, Game.opening_id == Opening.opening_id)
        .filter((Game.white_id == player_id) | (Game.black_id == player_id))
        .group_by(Opening.opening_id)
        .order_by(func.count(Game.game_id).desc())
    )
    if limit:
        q = q.limit(limit)
    return q.all()


def _self_player():
    return Player.query.filter_by(is_self=True).first()


def _recent_games(player_ids=None, limit=15):
    q = Game.query.order_by(Game.played_at.desc())
    if player_ids:
        q = q.filter((Game.white_id.in_(player_ids)) | (Game.black_id.in_(player_ids)))
    return q.limit(limit).all()


def _sparkline_svg(points, width=220, height=56, pad=8):
    """Render a compact single-series trend sparkline as inline SVG.

    points: list of (label, value) in chronological order. Uses CSS custom
    properties (var(--accent), var(--bg2), var(--text2)) so it themes with the
    page's existing dark/light mode automatically. 2px line, >=8px end-marker
    with a surface-color ring, direct end-label — no gridlines/legend, this is
    a compact stat-card sparkline, not a full chart (per the dataviz skill).
    """
    if len(points) < 2:
        return ""
    values = [v for _, v in points]
    lo, hi = min(values), max(values)
    if lo == hi:
        lo, hi = lo - 1, hi + 1

    def x_at(i):
        return pad + i / (len(points) - 1) * (width - 2 * pad)

    def y_at(v):
        return height - pad - (v - lo) / (hi - lo) * (height - 2 * pad)

    coords = [(x_at(i), y_at(v)) for i, (_, v) in enumerate(points)]
    poly = " ".join(f"{x:.1f},{y:.1f}" for x, y in coords)
    last_x, last_y = coords[-1]
    last_label, last_value = points[-1]

    return f'''<svg viewBox="0 0 {width} {height}" class="sparkline" role="img" aria-label="Rating trend, currently {last_value}">
  <polyline points="{poly}" fill="none" stroke="var(--accent)" stroke-width="2" stroke-linejoin="round" stroke-linecap="round" />
  <circle cx="{last_x:.1f}" cy="{last_y:.1f}" r="6" fill="var(--bg2)" />
  <circle cx="{last_x:.1f}" cy="{last_y:.1f}" r="4" fill="var(--accent)">
    <title>{last_label}: {last_value}</title>
  </circle>
  <text x="{min(last_x + 8, width - 4)}" y="{last_y + 4:.1f}" text-anchor="{'end' if last_x + 8 > width - 30 else 'start'}" class="sparkline-label">{last_value}</text>
</svg>'''


def _rating_trend(player_id, limit=200):
    """Chronological (rating, played_at) points per time_class for a player.

    Returns {time_class: [(played_at, rating), ...]}. Ratings from different time
    classes (bullet ~1500, rapid ~1800) must never be plotted on one merged line.
    """
    rating_case = case(
        (Game.white_id == player_id, Game.white_rating),
        else_=Game.black_rating,
    )
    rows = (
        db.session.query(Game.played_at, Game.time_class, rating_case.label("rating"))
        .filter((Game.white_id == player_id) | (Game.black_id == player_id))
        .filter(rating_case.isnot(None))
        .filter(Game.played_at.isnot(None))
        .order_by(Game.played_at.desc())
        .limit(limit)
        .all()
    )
    by_class = {}
    for played_at, time_class, rating in reversed(rows):
        by_class.setdefault(time_class or "unknown", []).append((played_at, rating))
    return by_class


def _pro_games_query():
    """Games where either player has the 'pro' tag, or the game is a tournament
    broadcast — the two ingestion paths that feed the unified /pros feed."""
    from pro_accounts import PRO_TAG_NAME

    pro_tag = Tag.query.filter_by(name=PRO_TAG_NAME).first()
    if not pro_tag:
        return Game.query.filter(Game.source == "broadcast")
    return Game.query.filter(
        Game.white.has(Player.tags.any(Tag.tag_id == pro_tag.tag_id))
        | Game.black.has(Player.tags.any(Tag.tag_id == pro_tag.tag_id))
        | (Game.source == "broadcast")
    )


@event.listens_for(Engine, "connect")
def _set_sqlite_wal(dbapi_connection, connection_record):
    # WAL lets a background analysis thread commit per-ply progress without
    # locking out normal request traffic (default rollback-journal mode
    # takes a whole-DB lock per writer).
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.close()


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)
    db.init_app(app)

    with app.app_context():
        db.create_all()
        # Self-heal: a deploy restart (systemctl restart) kills any in-flight
        # background analysis thread, leaving its GameAnalysis row stuck at
        # analyzed_at=NULL forever (the kickoff route treats an existing NULL
        # row as "already running" and refuses to restart it). Clear these on
        # boot so the game becomes analyzable again.
        stuck = GameAnalysis.query.filter_by(analyzed_at=None).all()
        for row in stuck:
            MoveEval.query.filter_by(game_id=row.game_id).delete()
            db.session.delete(row)
        if stuck:
            db.session.commit()

    # ── routes ───────────────────────────────────────────────────────────

    @app.route("/")
    def dashboard():
        self_player = _self_player()
        if not self_player:
            return redirect(url_for("games_list"))

        record = _player_record(self_player.player_id)
        recent_games = _recent_games(player_ids=[self_player.player_id], limit=10)
        top_openings = _opening_breakdown(self_player.player_id, limit=5)

        trend = _rating_trend(self_player.player_id, limit=200)
        sparklines = {}
        for time_class, points in trend.items():
            date_value_pairs = [
                (played_at.strftime("%Y-%m-%d") if played_at else "?", rating)
                for played_at, rating in points
            ]
            svg = _sparkline_svg(date_value_pairs)
            if svg:
                sparklines[time_class] = {"svg": svg, "current": points[-1][1]}

        friend_count = Player.query.filter_by(is_friend=True).count()
        total_games = Game.query.count()

        recent_pro_games = _pro_games_query().order_by(Game.played_at.desc()).limit(5).all()

        return render_template(
            "dashboard.html",
            self_player=self_player,
            record=record,
            recent_games=recent_games,
            top_openings=top_openings,
            sparklines=sparklines,
            friend_count=friend_count,
            total_games=total_games,
            recent_pro_games=recent_pro_games,
        )

    # ── games ────────────────────────────────────────────────────────────

    @app.route("/games")
    def games_list():
        page = request.args.get("page", 1, type=int)
        per_page = 50

        q = Game.query.order_by(Game.played_at.desc())

        # filters
        player = request.args.get("player", "").strip()
        if player:
            q = q.filter(
                (Game.white.has(Player.display_name.ilike(f"%{player}%")))
                | (Game.black.has(Player.display_name.ilike(f"%{player}%")))
            )

        time_class = request.args.get("time_class")
        if time_class:
            q = q.filter(Game.time_class == time_class)

        result = request.args.get("result")
        if result:
            q = q.filter(Game.result == result)

        opening = request.args.get("opening", "").strip()
        if opening:
            q = q.filter(Game.opening.has(Opening.name.ilike(f"%{opening}%")))

        opening_id = request.args.get("opening_id", type=int)
        opening_filter = None
        if opening_id:
            q = q.filter(Game.opening_id == opening_id)
            opening_filter = Opening.query.get(opening_id)

        source = request.args.get("source")
        if source:
            q = q.filter(Game.source == source)

        tag_name = request.args.get("tag")
        if tag_name:
            q = q.filter(Game.tags.any(Tag.name == tag_name))

        pagination = q.paginate(page=page, per_page=per_page, error_out=False)

        time_classes = [r[0] for r in db.session.query(Game.time_class).distinct().order_by(Game.time_class) if r[0]]
        tags = Tag.query.order_by(Tag.name).all()

        filters_without_opening = {k: v for k, v in request.args.items() if k != "opening_id"}

        return render_template(
            "games.html",
            games=pagination.items,
            pagination=pagination,
            time_classes=time_classes,
            tags=tags,
            filters=request.args,
            opening_filter=opening_filter,
            filters_without_opening=filters_without_opening,
        )

    @app.route("/games/<int:game_id>")
    def game_detail(game_id):
        game = Game.query.get_or_404(game_id)
        tags = Tag.query.order_by(Tag.name).all()
        return render_template("game_detail.html", game=game, all_tags=tags)

    @app.route("/games/<int:game_id>/notes", methods=["POST"])
    def update_notes(game_id):
        game = Game.query.get_or_404(game_id)
        game.notes = request.form.get("notes", "")
        db.session.commit()
        flash("Notes saved.")
        return redirect(url_for("game_detail", game_id=game_id))

    # ── on-demand analysis ──────────────────────────────────────────────────

    @app.route("/games/<int:game_id>/analyze", methods=["POST"])
    def analyze_game(game_id):
        Game.query.get_or_404(game_id)
        existing = GameAnalysis.query.get(game_id)
        if existing:
            return jsonify(status="done" if existing.analyzed_at else "in_progress")

        row = GameAnalysis(game_id=game_id)
        db.session.add(row)
        try:
            db.session.commit()
        except IntegrityError:
            # Lost a race to a concurrent double-click — someone else's insert won.
            db.session.rollback()
            return jsonify(status="in_progress")

        import analysis
        threading.Thread(target=analysis.run_full_analysis, args=(app, game_id), daemon=True).start()
        return jsonify(status="started"), 202

    @app.route("/games/<int:game_id>/analysis")
    def game_analysis_status(game_id):
        row = GameAnalysis.query.get(game_id)
        if not row:
            return jsonify(status="not_started")
        if row.error:
            return jsonify(status="error", error=row.error)
        if row.analyzed_at is None:
            return jsonify(status="in_progress", plies_done=row.plies_done, ply_total=row.ply_total)
        moves = MoveEval.query.filter_by(game_id=game_id).order_by(MoveEval.ply).all()
        return jsonify(
            status="done",
            white_acpl=row.white_acpl,
            black_acpl=row.black_acpl,
            moves=[
                {
                    "ply": m.ply,
                    "score_cp": m.score_cp,
                    "mate_in": m.mate_in,
                    "best_move_san": m.best_move_san,
                    "classification": m.classification,
                }
                for m in moves
            ],
        )

    @app.route("/api/analyze-position", methods=["POST"])
    def analyze_position_route():
        fen = request.json.get("fen")
        game_id = request.json.get("game_id")
        ply = request.json.get("ply")
        if not fen:
            return jsonify(error="fen required"), 400

        if game_id is not None and ply is not None:
            game_analysis = GameAnalysis.query.get(game_id)
            if game_analysis and game_analysis.analyzed_at:
                cached = MoveEval.query.filter_by(game_id=game_id, ply=ply).first()
                if cached:
                    return jsonify(
                        cached=True,
                        score_cp=cached.score_cp,
                        mate_in=cached.mate_in,
                        best_move_san=cached.best_move_san,
                    )

        import analysis
        try:
            result = analysis.analyze_position(fen, app.config["STOCKFISH_PATH"])
        except ValueError:
            return jsonify(error="invalid fen"), 400
        return jsonify(
            cached=False,
            score_cp=result["score_cp"],
            mate_in=result["mate_in"],
            best_move_san=result["best_san"],
        )

    # ── tags ─────────────────────────────────────────────────────────────

    @app.route("/api/tags", methods=["POST"])
    def create_tag():
        name = request.json.get("name", "").strip().lower()
        color = request.json.get("color", "#6366f1")
        if not name:
            return jsonify(error="name required"), 400
        existing = Tag.query.filter_by(name=name).first()
        if existing:
            return jsonify(tag_id=existing.tag_id, name=existing.name, color=existing.color)
        tag = Tag(name=name, color=color)
        db.session.add(tag)
        db.session.commit()
        return jsonify(tag_id=tag.tag_id, name=tag.name, color=tag.color), 201

    @app.route("/api/games/<int:game_id>/tags", methods=["POST"])
    def toggle_game_tag(game_id):
        game = Game.query.get_or_404(game_id)
        tag_id = request.json.get("tag_id")
        tag = Tag.query.get_or_404(tag_id)
        if tag in game.tags:
            game.tags.remove(tag)
            action = "removed"
        else:
            game.tags.append(tag)
            action = "added"
        db.session.commit()
        return jsonify(action=action, tag_id=tag.tag_id)

    @app.route("/api/players/<int:player_id>/tags", methods=["POST"])
    def toggle_player_tag(player_id):
        player = Player.query.get_or_404(player_id)
        tag_id = request.json.get("tag_id")
        tag = Tag.query.get_or_404(tag_id)
        if tag in player.tags:
            player.tags.remove(tag)
            action = "removed"
        else:
            player.tags.append(tag)
            action = "added"
        db.session.commit()
        return jsonify(action=action, tag_id=tag.tag_id)

    # ── collections ──────────────────────────────────────────────────────

    @app.route("/collections")
    def collections_list():
        colls = Collection.query.order_by(Collection.created_at.desc()).all()
        return render_template("collections.html", collections=colls)

    @app.route("/collections/<int:coll_id>")
    def collection_detail(coll_id):
        coll = Collection.query.get_or_404(coll_id)
        return render_template("collection_detail.html", collection=coll)

    @app.route("/collections/new", methods=["POST"])
    def create_collection():
        name = request.form.get("name", "").strip()
        if not name:
            flash("Collection name required.")
            return redirect(url_for("collections_list"))
        coll = Collection(name=name, description=request.form.get("description", ""))
        db.session.add(coll)
        db.session.commit()
        flash(f"Created collection '{name}'.")
        return redirect(url_for("collections_list"))

    @app.route("/api/games/<int:game_id>/collections", methods=["POST"])
    def toggle_game_collection(game_id):
        game = Game.query.get_or_404(game_id)
        coll_id = request.json.get("collection_id")
        coll = Collection.query.get_or_404(coll_id)
        if coll in game.collections:
            game.collections.remove(coll)
            action = "removed"
        else:
            game.collections.append(coll)
            action = "added"
        db.session.commit()
        return jsonify(action=action)

    # ── players ──────────────────────────────────────────────────────────

    @app.route("/players")
    def players_list():
        show = request.args.get("show", "known")
        q = request.args.get("q", "").strip()
        tag_name = request.args.get("tag", "").strip()
        page = request.args.get("page", 1, type=int)

        # Count games per player in one pass over `game` (white + black union),
        # rather than joining game to player per-row — much cheaper at scale.
        game_counts = (
            db.session.query(Game.white_id.label("player_id"))
            .union_all(db.session.query(Game.black_id.label("player_id")))
            .subquery()
        )
        counts_subq = (
            db.session.query(
                game_counts.c.player_id,
                func.count().label("game_count"),
            )
            .group_by(game_counts.c.player_id)
            .subquery()
        )

        base = db.session.query(
            Player,
            func.coalesce(counts_subq.c.game_count, 0).label("game_count"),
        ).outerjoin(counts_subq, counts_subq.c.player_id == Player.player_id)

        if tag_name:
            base = base.filter(Player.tags.any(Tag.name == tag_name))

        if q:
            base = base.filter(Player.display_name.ilike(f"%{q}%"))
        elif show != "all" and not tag_name:
            base = base.filter((Player.is_self.is_(True)) | (Player.is_friend.is_(True)))

        base = base.order_by(
            Player.is_self.desc(), Player.is_friend.desc(), Player.display_name
        )

        pagination = None
        if show == "all" or q or tag_name:
            pagination = base.paginate(page=page, per_page=50, error_out=False)
            players = pagination.items
        else:
            players = base.all()

        all_tags = Tag.query.order_by(Tag.name).all()

        return render_template(
            "players.html",
            players=players,
            pagination=pagination,
            show=show,
            q=q,
            tag_name=tag_name,
            all_tags=all_tags,
        )

    @app.route("/players/<int:player_id>")
    def player_detail(player_id):
        player = Player.query.get_or_404(player_id)
        games = (
            Game.query
            .filter((Game.white_id == player_id) | (Game.black_id == player_id))
            .order_by(Game.played_at.desc())
            .limit(100)
            .all()
        )

        record = _player_record(player_id)

        by_time_class = (
            db.session.query(Game.time_class, func.count().label("n"))
            .filter((Game.white_id == player_id) | (Game.black_id == player_id))
            .group_by(Game.time_class)
            .order_by(func.count().desc())
            .all()
        )

        top_openings = _opening_breakdown(player_id, limit=8)

        head_to_head = None
        self_player = _self_player()
        if self_player and self_player.player_id != player_id:
            h2h_record = _player_record(self_player.player_id, opponent_id=player_id)
            if h2h_record["total"] > 0:
                head_to_head = h2h_record

        all_tags = Tag.query.order_by(Tag.name).all()

        return render_template(
            "player_detail.html",
            player=player,
            games=games,
            record=record,
            by_time_class=by_time_class,
            top_openings=top_openings,
            head_to_head=head_to_head,
            all_tags=all_tags,
        )

    @app.route("/players/<int:player_id>/openings")
    def player_openings(player_id):
        player = Player.query.get_or_404(player_id)
        openings = _opening_breakdown(player_id)
        return render_template("player_openings.html", player=player, openings=openings)

    @app.route("/players/<int:player_id>/friend", methods=["POST"])
    def toggle_friend(player_id):
        player = Player.query.get_or_404(player_id)
        player.is_friend = not player.is_friend
        db.session.commit()
        return redirect(url_for("player_detail", player_id=player_id))

    # ── pro games ────────────────────────────────────────────────────────

    @app.route("/pros")
    def pro_games():
        page = request.args.get("page", 1, type=int)
        q = _pro_games_query()
        pagination = q.order_by(Game.played_at.desc()).paginate(page=page, per_page=50, error_out=False)
        return render_template("pro_games.html", games=pagination.items, pagination=pagination)

    # ── sync ─────────────────────────────────────────────────────────────

    @app.route("/sync")
    def sync_page():
        return render_template("sync.html")

    @app.route("/sync/chesscom", methods=["POST"])
    def sync_chesscom():
        from sync.chesscom import sync_user

        username = request.form.get("username", "").strip()
        if not username:
            flash("Username required.")
            return redirect(url_for("sync_page"))
        result = sync_user(username)
        if "error" in result:
            flash(result["error"], "error")
            return redirect(url_for("sync_page"))
        flash(f"Synced {username}: {result['new_games']} new games from {result['archives']} archives.")
        return redirect(url_for("player_detail", player_id=result["player_id"]))

    @app.route("/sync/lichess", methods=["POST"])
    def sync_lichess():
        from sync.lichess import sync_user

        username = request.form.get("username", "").strip()
        if not username:
            flash("Username required.")
            return redirect(url_for("sync_page"))
        result = sync_user(username)
        if "error" in result:
            flash(result["error"], "error")
            return redirect(url_for("sync_page"))
        flash(f"Synced {username}: {result['new_games']} new games ({result['total_games']} seen).")
        return redirect(url_for("player_detail", player_id=result["player_id"]))

    # ── CLI commands ─────────────────────────────────────────────────────

    @app.cli.command("init-db")
    def init_db():
        """Create all tables."""
        db.create_all()
        click.echo("Database initialized.")

    @app.cli.command("sync-chesscom")
    @click.argument("usernames", nargs=-1, required=True)
    def cli_sync_chesscom(usernames):
        """Sync one or more chess.com users."""
        import logging
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
        from sync.chesscom import sync_user

        for u in usernames:
            result = sync_user(u)
            click.echo(f"{u}: {result}")

    @app.cli.command("sync-lichess")
    @click.argument("usernames", nargs=-1, required=True)
    def cli_sync_lichess(usernames):
        """Sync one or more Lichess users."""
        import logging
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
        from sync.lichess import sync_user

        for u in usernames:
            result = sync_user(u)
            click.echo(f"{u}: {result}")

    @app.cli.command("sync-pros")
    def cli_sync_pros():
        """Sync the curated pro/streamer accounts (pro_accounts.py) and tag them 'pro'."""
        import logging
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
        import importlib

        from pro_accounts import PRO_ACCOUNTS, PRO_TAG_NAME

        pro_tag = Tag.query.filter_by(name=PRO_TAG_NAME).first()
        if not pro_tag:
            pro_tag = Tag(name=PRO_TAG_NAME, color="#f59e0b")
            db.session.add(pro_tag)
            db.session.commit()

        for acct in PRO_ACCOUNTS:
            mod = importlib.import_module(f"sync.{acct['source']}")
            # Recent games only — pros often have years of blitz/bullet history
            # on-platform, and this feed only wants "recent", not a full backfill.
            kwargs = {"max_archives": 2} if acct["source"] == "chesscom" else {}
            result = mod.sync_user(acct["username"], **kwargs)
            if "player_id" in result:
                player = Player.query.get(result["player_id"])
                if pro_tag not in player.tags:
                    player.tags.append(pro_tag)
                    db.session.commit()
            click.echo(f"{acct['username']}: {result}")

    @app.cli.command("sync-broadcasts")
    def cli_sync_broadcasts():
        """Sync the curated major-tournament broadcasts (broadcast_tournaments.py)."""
        import logging
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
        from sync.broadcasts import sync_all

        for result in sync_all():
            click.echo(result)

    @app.cli.command("mark-self")
    @click.argument("username")
    @click.option("--source", default="chesscom", type=click.Choice(["chesscom", "lichess"]), help="Account source.")
    def cli_mark_self(username, source):
        """Flag a chess.com/Lichess username as 'you'."""
        ident = PlayerIdentity.query.filter_by(source=source, username=username).first()
        if not ident:
            click.echo(f"No player found for {source}/{username}. Sync first.")
            return
        # Clear any existing is_self
        Player.query.filter_by(is_self=True).update({"is_self": False})
        ident.player.is_self = True
        db.session.commit()
        click.echo(f"Marked {ident.player.display_name} as self.")

    @app.cli.command("mark-friend")
    @click.argument("username")
    @click.option("--source", default="chesscom", type=click.Choice(["chesscom", "lichess"]), help="Account source.")
    def cli_mark_friend(username, source):
        """Flag a chess.com/Lichess username as a friend."""
        ident = PlayerIdentity.query.filter_by(source=source, username=username).first()
        if not ident:
            click.echo(f"No player found for {source}/{username}. Sync first.")
            return
        ident.player.is_friend = True
        db.session.commit()
        click.echo(f"Marked {ident.player.display_name} as friend.")

    @app.cli.command("link-identity")
    @click.argument("player_id", type=int)
    @click.argument("source", type=click.Choice(["chesscom", "lichess"]))
    @click.argument("username")
    def cli_link_identity(player_id, source, username):
        """Attach another account (e.g. a Lichess username) to an existing player.

        Use this to merge a friend's Lichess and chess.com accounts into one
        Player record, e.g.: flask link-identity 18240 lichess nomadchessty
        """
        player = Player.query.get(player_id)
        if not player:
            click.echo(f"No player with id {player_id}.")
            return
        existing = PlayerIdentity.query.filter_by(source=source, username=username).first()
        if existing:
            click.echo(f"{source}/{username} is already linked to player {existing.player_id} ({existing.player.display_name}).")
            return
        db.session.add(PlayerIdentity(player_id=player_id, source=source, username=username))
        db.session.commit()
        click.echo(f"Linked {source}/{username} to {player.display_name} (player {player_id}).")

    return app


app = create_app()

if __name__ == "__main__":
    app.run(debug=True)
