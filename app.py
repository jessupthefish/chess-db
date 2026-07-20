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

import stats
from charts import bar_chart_svg, line_chart_svg, sparkline_svg
from config import Config
from models import (
    Collection,
    Event,
    Game,
    GameAnalysis,
    GamePosition,
    MoveEval,
    MoveEvalLine,
    Opening,
    OpeningNode,
    Player,
    PlayerIdentity,
    Tag,
    collection_game,
    db,
    game_tag,
)


def _player_record(player_id, opponent_id=None, collection_id=None):
    """Aggregate win/loss/draw record for a player, optionally restricted to games
    against one specific opponent (head-to-head) or to one collection's games.
    Pure SQL aggregation — avoids loading full Game rows (incl. the pgn text
    column) just to tally results."""
    filters = [(Game.white_id == player_id) | (Game.black_id == player_id)]
    if opponent_id is not None:
        filters.append((Game.white_id == opponent_id) | (Game.black_id == opponent_id))

    win_case, loss_case, draw_case = stats.result_cases(player_id)

    q = db.session.query(
        func.coalesce(func.sum(win_case), 0),
        func.coalesce(func.sum(loss_case), 0),
        func.coalesce(func.sum(draw_case), 0),
        func.count(),
    ).select_from(Game)
    if collection_id is not None:
        q = q.join(collection_game, collection_game.c.game_id == Game.game_id).filter(
            collection_game.c.collection_id == collection_id
        )
    wins, losses, draws, total = q.filter(*filters).one()
    return {"wins": wins, "losses": losses, "draws": draws, "total": total}


def _opening_breakdown(player_id, limit=None, collection_id=None):
    """Per-opening win/loss/draw breakdown for a player, most-played first.
    collection_id restricts to that collection's games."""
    win_case, loss_case, draw_case = stats.result_cases(player_id)

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
    if collection_id is not None:
        q = q.join(collection_game, collection_game.c.game_id == Game.game_id).filter(
            collection_game.c.collection_id == collection_id
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


# _sparkline_svg moved to charts.py (as sparkline_svg) alongside the new
# full-size chart primitives the /stats page uses.


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


def _pro_condition():
    """SQLAlchemy boolean expression for 'this game belongs to the pro/broadcast
    feed' — either player has the 'pro' tag, or the game is a tournament
    broadcast. Factored out of _pro_games_query() so the opening-explorer's
    mine/pro scope filter can reuse it in a join instead of duplicating it."""
    from pro_accounts import PRO_TAG_NAME

    pro_tag = Tag.query.filter_by(name=PRO_TAG_NAME).first()
    if not pro_tag:
        return Game.source == "broadcast"
    return (
        Game.white.has(Player.tags.any(Tag.tag_id == pro_tag.tag_id))
        | Game.black.has(Player.tags.any(Tag.tag_id == pro_tag.tag_id))
        | (Game.source == "broadcast")
    )


def _pro_games_query():
    """Games where either player has the 'pro' tag, or the game is a tournament
    broadcast — the two ingestion paths that feed the unified /pros feed."""
    return Game.query.filter(_pro_condition())


def _opening_tree_children(node_id, scope="mine"):
    """Per-child-move aggregated stats for an opening-tree node: game count,
    result breakdown, and avg rating, scoped to either the self player's games
    or the pro/broadcast feed. Mirrors _opening_breakdown's color-aware case()
    pattern. 'pro' scope has no fixed 'self' side, so it's framed as White/Black
    win% rather than W/L — see opening_explorer.html."""
    if scope == "pro":
        scope_filter = _pro_condition()
        win_case = case((Game.result == "1-0", 1), else_=0)   # White win
        loss_case = case((Game.result == "0-1", 1), else_=0)  # Black win
        rating_expr = (Game.white_rating + Game.black_rating) / 2.0
    else:
        self_player = _self_player()
        if not self_player:
            return []
        pid = self_player.player_id
        scope_filter = (Game.white_id == pid) | (Game.black_id == pid)
        win_case, loss_case, _ = stats.result_cases(pid)
        rating_expr = case((Game.white_id == pid, Game.white_rating), else_=Game.black_rating)

    draw_case = case((Game.result == "1/2-1/2", 1), else_=0)

    return (
        db.session.query(
            OpeningNode.node_id,
            OpeningNode.move_san,
            Opening.eco,
            Opening.name,
            func.count(func.distinct(GamePosition.game_id)).label("total"),
            func.coalesce(func.sum(win_case), 0).label("wins"),
            func.coalesce(func.sum(loss_case), 0).label("losses"),
            func.coalesce(func.sum(draw_case), 0).label("draws"),
            func.avg(rating_expr).label("avg_rating"),
        )
        .join(GamePosition, GamePosition.node_id == OpeningNode.node_id)
        .join(Game, Game.game_id == GamePosition.game_id)
        .outerjoin(Opening, Opening.opening_id == OpeningNode.opening_id)
        .filter(OpeningNode.parent_id == node_id)
        .filter(scope_filter)
        .group_by(OpeningNode.node_id)
        .order_by(func.count(func.distinct(GamePosition.game_id)).desc())
        .all()
    )


def _opening_tree_games(node_id, scope="mine", page=1, per_page=50):
    """Paginated list of games that passed through a given opening-tree node,
    scoped like _opening_tree_children. Returns None if scope='mine' and no
    self player is set (mirrors the dashboard's no-self-player guard)."""
    q = (
        Game.query
        .join(GamePosition, GamePosition.game_id == Game.game_id)
        .filter(GamePosition.node_id == node_id)
    )
    if scope == "pro":
        q = q.filter(_pro_condition())
    else:
        self_player = _self_player()
        if not self_player:
            return None
        q = q.filter((Game.white_id == self_player.player_id) | (Game.black_id == self_player.player_id))
    q = q.order_by(Game.played_at.desc())
    return q.paginate(page=page, per_page=per_page, error_out=False)


def _opening_totals(scope="mine"):
    """opening_id -> total game count, scoped like _opening_tree_children.
    Flat (per-Opening, not per-tree-node) — used to compare 'how often pros
    play this opening' against 'how often you do' for the prep-gaps view."""
    if scope == "pro":
        q = db.session.query(Game.opening_id, func.count().label("total")).filter(_pro_condition())
    else:
        self_player = _self_player()
        if not self_player:
            return {}
        pid = self_player.player_id
        q = db.session.query(Game.opening_id, func.count().label("total")).filter(
            (Game.white_id == pid) | (Game.black_id == pid)
        )
    q = q.filter(Game.opening_id.isnot(None)).group_by(Game.opening_id)
    return dict(q.all())


def _repertoire_gaps(min_games=5, pro_min_games=20, gap_ratio=10):
    """Two prep-oriented views built on top of the existing flat per-opening
    breakdown: (1) openings you have a poor record in (win rate ascending,
    with a min-game floor so a single loss doesn't dominate), and (2) openings
    pros play often that you rarely reach (pro game count >= pro_min_games,
    and either you have zero games in it or pros outnumber you by
    gap_ratio+)."""
    self_player = _self_player()
    needs_improvement = []
    if self_player:
        for opening_id, eco, name, total, wins, losses, draws in _opening_breakdown(self_player.player_id):
            if total < min_games:
                continue
            decisive = wins + losses
            win_rate = (wins / decisive * 100) if decisive else None
            needs_improvement.append({
                "opening_id": opening_id, "eco": eco, "name": name,
                "total": total, "wins": wins, "losses": losses, "draws": draws,
                "win_rate": win_rate,
            })
        needs_improvement.sort(key=lambda r: r["win_rate"] if r["win_rate"] is not None else 100)

    mine_totals = _opening_totals("mine")
    pro_totals = _opening_totals("pro")
    openings_by_id = {}
    if pro_totals:
        openings_by_id = {
            o.opening_id: o for o in Opening.query.filter(Opening.opening_id.in_(pro_totals.keys())).all()
        }

    prep_gaps = []
    for opening_id, pro_total in pro_totals.items():
        if pro_total < pro_min_games:
            continue
        mine_total = mine_totals.get(opening_id, 0)
        if mine_total == 0 or pro_total / max(mine_total, 1) >= gap_ratio:
            opening = openings_by_id.get(opening_id)
            prep_gaps.append({
                "opening_id": opening_id,
                "eco": opening.eco if opening else None,
                "name": opening.name if opening else None,
                "pro_total": pro_total,
                "mine_total": mine_total,
            })
    prep_gaps.sort(key=lambda r: r["pro_total"], reverse=True)

    return {"needs_improvement": needs_improvement[:20], "prep_gaps": prep_gaps[:20]}


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
            MoveEvalLine.query.filter_by(game_id=row.game_id).delete()
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
            svg = sparkline_svg(date_value_pairs)
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

    @app.route("/stats")
    def stats_page():
        self_player = _self_player()
        if not self_player:
            flash("Mark yourself first (flask mark-self) to see personal stats.", "error")
            return redirect(url_for("games_list"))
        pid = self_player.player_id
        time_class = request.args.get("time_class") or None

        record = _player_record(pid)
        streak = stats.streaks(pid, time_class)
        by_color = stats.results_by_color(pid, time_class)
        by_tc = stats.results_by_time_class(pid) if not time_class else []
        terminations = stats.results_by_termination(pid, time_class)

        rating_charts = {
            tc: line_chart_svg(points, aria_label=f"{tc} rating history")
            for tc, points in stats.rating_history(pid, time_class).items()
        }

        activity_years, activity_max = stats.monthly_activity(pid, time_class)

        dow = stats.results_by_dow(pid, time_class)
        dow_chart = bar_chart_svg(
            [(d["label"], d["score"] or 0, f"{d['label']}: {d['score']}% score over {d['total']} games")
             for d in dow if d["total"]],
            height=190, percent=True, ref_value=50, ref_label="50%",
            aria_label="Score percentage by day of week",
        )
        hours = stats.results_by_hour(pid, time_class)
        hour_chart = bar_chart_svg(
            [(h["label"], h["score"] or 0, f"{h['label']}:00 — {h['score']}% score over {h['total']} games")
             for h in hours if h["total"]],
            height=190, percent=True, ref_value=50, ref_label="50%",
            aria_label="Score percentage by hour of day",
        )

        bands = stats.vs_rating_bands(pid, time_class)
        bands_chart = bar_chart_svg(
            [(b["label"], b["score"] or 0, f"vs {b['band']}–{b['band'] + 99}: {b['score']}% score over {b['total']} games")
             for b in bands],
            height=190, percent=True, ref_value=50, ref_label="50%",
            aria_label="Score percentage by opponent rating band",
        )

        lengths = stats.game_length_distribution(pid, time_class)
        length_chart = bar_chart_svg(
            [(g["label"], g["total"], f"{g['label']} moves: {g['total']} games, {g['score']}% score")
             for g in lengths],
            height=190,
            aria_label="Game count by length in moves",
        )

        insights = stats.analysis_insights(pid, time_class)
        acpl_chart = ""
        if insights and len(insights["acpl_trend"]) >= 2:
            acpl_chart = line_chart_svg(
                insights["acpl_trend"], height=190,
                aria_label="Average centipawn loss per analyzed game over time",
            )

        return render_template(
            "stats.html",
            self_player=self_player,
            time_class=time_class,
            time_classes=stats.available_time_classes(pid),
            record=record,
            score=stats.score_pct(record["wins"], record["draws"], record["total"]),
            streak=streak,
            by_color=by_color,
            by_tc=by_tc,
            terminations=terminations,
            rating_charts=rating_charts,
            activity_years=activity_years,
            activity_max=activity_max,
            dow_chart=dow_chart,
            hour_chart=hour_chart,
            bands_chart=bands_chart,
            length_chart=length_chart,
            insights=insights,
            acpl_chart=acpl_chart,
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
        # MoveEval.ply is a move-number (1..N); MoveEvalLine.ply is a positions[]-index
        # (0..N) of the *pre-move* position — so move m's alternatives live at m.ply - 1.
        lines_by_ply = {}
        for l in MoveEvalLine.query.filter_by(game_id=game_id).order_by(MoveEvalLine.ply, MoveEvalLine.rank).all():
            lines_by_ply.setdefault(l.ply, []).append(l)
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
                    "lines": [
                        {
                            "rank": l.rank,
                            "score_cp": l.score_cp,
                            "mate_in": l.mate_in,
                            "best_move_uci": l.best_move_uci,
                            "best_move_san": l.best_move_san,
                            "preview_san": l.pv_san,
                        }
                        for l in lines_by_ply.get(m.ply - 1, [])
                    ],
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
                cached = (
                    MoveEvalLine.query.filter_by(game_id=game_id, ply=ply)
                    .order_by(MoveEvalLine.rank)
                    .all()
                )
                if cached:
                    top = cached[0]
                    return jsonify(
                        cached=True,
                        score_cp=top.score_cp,
                        mate_in=top.mate_in,
                        best_move_san=top.best_move_san,
                        lines=[
                            {
                                "rank": l.rank,
                                "score_cp": l.score_cp,
                                "mate_in": l.mate_in,
                                "best_move_uci": l.best_move_uci,
                                "best_move_san": l.best_move_san,
                                "preview_san": l.pv_san,
                            }
                            for l in cached
                        ],
                    )

        import analysis
        try:
            result_lines = analysis.analyze_position(fen, app.config["STOCKFISH_PATH"])
        except ValueError:
            return jsonify(error="invalid fen"), 400
        top = result_lines[0]
        return jsonify(
            cached=False,
            score_cp=top["score_cp"],
            mate_in=top["mate_in"],
            best_move_san=top["best_san"],
            lines=[
                {
                    "rank": l["rank"],
                    "score_cp": l["score_cp"],
                    "mate_in": l["mate_in"],
                    "best_move_uci": l["best_uci"],
                    "best_move_san": l["best_san"],
                    "preview_san": " ".join(l["pv_san"]),
                }
                for l in result_lines
            ],
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

    def _collection_summary(coll, self_player):
        """Aggregate stats for a collection's games: totals + date range over
        every game in it, plus self-scoped record / rating span / openings /
        analysis summary when a self player exists. All pure SQL — never loads
        Game rows (or their pgn text) just to aggregate."""
        member = collection_game.c.collection_id == coll.collection_id
        base = (
            db.session.query(func.count(), func.min(Game.played_at), func.max(Game.played_at))
            .select_from(Game)
            .join(collection_game, collection_game.c.game_id == Game.game_id)
            .filter(member)
        )
        total, first, last = base.one()
        summary = {
            "total": total,
            "first": first,
            "last": last,
            "record": None,
            "score": None,
            "rating_lo": None,
            "rating_hi": None,
            "openings": [],
            "n_analyzed": 0,
            "avg_acpl": None,
        }
        if not self_player or not total:
            return summary
        pid = self_player.player_id

        summary["record"] = _player_record(pid, collection_id=coll.collection_id)
        r = summary["record"]
        summary["score"] = stats.score_pct(r["wins"], r["draws"], r["total"])
        summary["openings"] = _opening_breakdown(pid, limit=8, collection_id=coll.collection_id)

        self_rating = case((Game.white_id == pid, Game.white_rating), else_=Game.black_rating)
        lo, hi = (
            db.session.query(func.min(self_rating), func.max(self_rating))
            .select_from(Game)
            .join(collection_game, collection_game.c.game_id == Game.game_id)
            .filter(member)
            .filter((Game.white_id == pid) | (Game.black_id == pid))
            .one()
        )
        summary["rating_lo"], summary["rating_hi"] = lo, hi

        self_acpl = case(
            (Game.white_id == pid, GameAnalysis.white_acpl), else_=GameAnalysis.black_acpl
        )
        n_analyzed, avg_acpl = (
            db.session.query(func.count(), func.avg(self_acpl))
            .select_from(GameAnalysis)
            .join(Game, Game.game_id == GameAnalysis.game_id)
            .join(collection_game, collection_game.c.game_id == Game.game_id)
            .filter(member)
            .filter(GameAnalysis.analyzed_at.isnot(None))
            .one()
        )
        summary["n_analyzed"] = n_analyzed
        summary["avg_acpl"] = round(avg_acpl, 1) if avg_acpl is not None else None
        return summary

    @app.route("/collections")
    def collections_list():
        colls = Collection.query.order_by(Collection.created_at.desc()).all()
        # one COUNT GROUP BY instead of len(c.games), which would load every
        # Game row (incl. pgn text) per card
        counts = dict(
            db.session.query(collection_game.c.collection_id, func.count())
            .group_by(collection_game.c.collection_id)
            .all()
        )
        return render_template("collections.html", collections=colls, counts=counts)

    @app.route("/collections/<int:coll_id>")
    def collection_detail(coll_id):
        coll = Collection.query.get_or_404(coll_id)
        page = request.args.get("page", 1, type=int)
        games = (
            Game.query.join(collection_game, collection_game.c.game_id == Game.game_id)
            .filter(collection_game.c.collection_id == coll_id)
            .order_by(Game.played_at.desc())
            .paginate(page=page, per_page=50, error_out=False)
        )
        summary = _collection_summary(coll, _self_player())
        return render_template(
            "collection_detail.html", collection=coll, games=games, summary=summary
        )

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

    @app.route("/collections/<int:coll_id>/edit", methods=["POST"])
    def update_collection(coll_id):
        coll = Collection.query.get_or_404(coll_id)
        name = request.form.get("name", "").strip()
        if not name:
            flash("Collection name required.")
            return redirect(url_for("collection_detail", coll_id=coll_id))
        coll.name = name
        coll.description = request.form.get("description", "").strip()
        db.session.commit()
        flash("Collection updated.")
        return redirect(url_for("collection_detail", coll_id=coll_id))

    @app.route("/collections/<int:coll_id>/delete", methods=["POST"])
    def delete_collection(coll_id):
        coll = Collection.query.get_or_404(coll_id)
        name = coll.name
        db.session.delete(coll)  # SQLAlchemy clears the collection_game rows
        db.session.commit()
        flash(f"Deleted collection '{name}'.")
        return redirect(url_for("collections_list"))

    @app.route("/api/collections", methods=["POST"])
    def api_create_collection():
        """JSON create-or-get, mirroring create_tag() — used by the inline
        'new collection…' input in the collection picker popover."""
        name = (request.json.get("name") or "").strip()
        if not name:
            return jsonify(error="name required"), 400
        existing = Collection.query.filter(func.lower(Collection.name) == name.lower()).first()
        if existing:
            return jsonify(collection_id=existing.collection_id, name=existing.name)
        coll = Collection(name=name)
        db.session.add(coll)
        db.session.commit()
        return jsonify(collection_id=coll.collection_id, name=coll.name), 201

    @app.route("/api/games/<int:game_id>/collections")
    def game_collections(game_id):
        """All collections with membership flags for one game — feeds the
        collection picker popover."""
        game = Game.query.get_or_404(game_id)
        member_ids = {c.collection_id for c in game.collections}
        colls = Collection.query.order_by(Collection.name.asc()).all()
        return jsonify(
            collections=[
                {
                    "collection_id": c.collection_id,
                    "name": c.name,
                    "member": c.collection_id in member_ids,
                }
                for c in colls
            ]
        )

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

    # ── opening explorer ─────────────────────────────────────────────────

    @app.route("/openings")
    def opening_explorer_root():
        root = OpeningNode.query.filter_by(parent_id=None).first()
        if not root:
            return render_template("opening_explorer.html", node=None)
        scope = request.args.get("scope", "mine")
        return redirect(url_for("opening_explorer", node_id=root.node_id, scope=scope))

    @app.route("/openings/<int:node_id>")
    def opening_explorer(node_id):
        node = OpeningNode.query.get_or_404(node_id)
        scope = request.args.get("scope", "mine")
        if scope not in ("mine", "pro"):
            scope = "mine"

        breadcrumb = []
        n = node
        while n is not None:
            breadcrumb.append(n)
            n = n.parent
        breadcrumb.reverse()

        children = _opening_tree_children(node_id, scope=scope)

        page = request.args.get("page", 1, type=int)
        pagination = _opening_tree_games(node_id, scope=scope, page=page)

        return render_template(
            "opening_explorer.html",
            node=node,
            breadcrumb=breadcrumb,
            children=children,
            scope=scope,
            pagination=pagination,
            self_player=_self_player(),
        )

    @app.route("/openings/stats")
    def opening_stats():
        self_player = _self_player()
        gaps = _repertoire_gaps()
        top_openings = _opening_breakdown(self_player.player_id, limit=15) if self_player else []
        return render_template(
            "opening_stats.html",
            self_player=self_player,
            top_openings=top_openings,
            needs_improvement=gaps["needs_improvement"],
            prep_gaps=gaps["prep_gaps"],
        )

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
        import opening_tree
        import position_index
        opening_tree.rebuild_all()
        position_index.rebuild_all()
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
        import opening_tree
        import position_index
        opening_tree.rebuild_all()
        position_index.rebuild_all()
        flash(f"Synced {username}: {result['new_games']} new games ({result['total_games']} seen).")
        return redirect(url_for("player_detail", player_id=result["player_id"]))

    # ── blunder puzzles ──────────────────────────────────────────────────

    def _puzzle_candidates(pid):
        """MoveEval blunders made BY the self player in analyzed games.
        MoveEval.ply is the move number (1..N): odd ply => White moved."""
        self_moved = ((MoveEval.ply % 2 == 1) & (Game.white_id == pid)) | (
            (MoveEval.ply % 2 == 0) & (Game.black_id == pid)
        )
        return (
            db.session.query(MoveEval)
            .join(Game, Game.game_id == MoveEval.game_id)
            .join(GameAnalysis, GameAnalysis.game_id == Game.game_id)
            .filter(GameAnalysis.analyzed_at.isnot(None))
            .filter(MoveEval.classification == "blunder")
            .filter(self_moved)
        )

    def _puzzle_payload(me):
        """Build the puzzle JSON for one blunder MoveEval.

        Ply-convention bridge (same as game_analysis_status's
        lines_by_ply[m.ply - 1] lookup): MoveEval.ply is the move number m
        (1..N); the position BEFORE move m is positions[m - 1], which is the
        MoveEvalLine.ply convention. So the puzzle position is the replay to
        m - 1 plies, and the engine alternatives for the puzzle are the
        MoveEvalLine rows at ply == m - 1. me.best_move_uci/san already hold
        the engine best from that pre-move position.
        """
        import io as io_lib

        import chess.pgn as chess_pgn

        game = db.session.get(Game, me.game_id)
        pgn_game = chess_pgn.read_game(io_lib.StringIO(game.pgn))
        if pgn_game is None:
            return None
        board = pgn_game.board()
        played = None
        for i, move in enumerate(pgn_game.mainline_moves(), start=1):
            if i == me.ply:
                played = {"uci": move.uci(), "san": board.san(move)}
                break
            board.push(move)
        if played is None:
            return None

        lines = (
            MoveEvalLine.query.filter_by(game_id=me.game_id, ply=me.ply - 1)
            .order_by(MoveEvalLine.rank.asc())
            .all()
        )
        best_before = lines[0].score_cp if lines and lines[0].score_cp is not None else None
        swing_cp = None
        if best_before is not None and me.score_cp is not None:
            # both scores are white-POV; flip for a black mover
            swing_cp = best_before - me.score_cp
            if me.ply % 2 == 0:
                swing_cp = -swing_cp

        side = "white" if me.ply % 2 == 1 else "black"
        return {
            "game_id": me.game_id,
            "ply": me.ply,
            "move_no": (me.ply + 1) // 2,
            "fen": board.fen(),
            "side": side,
            "played_uci": played["uci"],
            "played_san": played["san"],
            "solution_uci": me.best_move_uci,
            "solution_san": me.best_move_san,
            "swing_cp": swing_cp,
            "alt_lines": [
                {
                    "rank": l.rank,
                    "uci": l.best_move_uci,
                    "san": l.best_move_san,
                    "score_cp": l.score_cp,
                    "mate_in": l.mate_in,
                    "preview_san": l.pv_san,
                }
                for l in lines
            ],
            "game_label": f"{game.white.display_name} vs {game.black.display_name}"
            + (f", {game.played_at.strftime('%Y-%m-%d')}" if game.played_at else ""),
            "game_url": url_for("game_detail", game_id=game.game_id),
        }

    def _puzzle_stats(pid):
        from models import PuzzleAttempt

        candidates = _puzzle_candidates(pid).count()
        attempts = PuzzleAttempt.query.count()
        solved = PuzzleAttempt.query.filter_by(correct=True).count()
        # current streak: walk recent attempts newest-first
        streak = 0
        for (correct,) in (
            db.session.query(PuzzleAttempt.correct)
            .order_by(PuzzleAttempt.attempted_at.desc(), PuzzleAttempt.attempt_id.desc())
            .limit(200)
            .all()
        ):
            if correct:
                streak += 1
            else:
                break
        return {
            "candidates": candidates,
            "attempts": attempts,
            "solved": solved,
            "solve_rate": round(solved / attempts * 100) if attempts else None,
            "streak": streak,
        }

    @app.route("/puzzles")
    def puzzles_page():
        self_player = _self_player()
        if not self_player:
            flash("Mark yourself first (flask mark-self) to train on your own blunders.", "error")
            return redirect(url_for("games_list"))
        return render_template("puzzles.html", stats=_puzzle_stats(self_player.player_id))

    @app.route("/api/puzzles/next")
    def next_puzzle():
        """Random blunder puzzle, preferring positions never solved yet.
        Returns the solution too — single-user app, checking happens client-
        side for instant feedback."""
        from models import PuzzleAttempt

        self_player = _self_player()
        if not self_player:
            return jsonify(error="no self player"), 400

        solved = (
            db.session.query(PuzzleAttempt.game_id, PuzzleAttempt.ply)
            .filter(PuzzleAttempt.correct.is_(True))
            .subquery()
        )
        q = _puzzle_candidates(self_player.player_id).outerjoin(
            solved, (solved.c.game_id == MoveEval.game_id) & (solved.c.ply == MoveEval.ply)
        )
        exclude = request.args.get("exclude", "")
        if ":" in exclude:
            eg, ep = exclude.split(":", 1)
            if eg.isdigit() and ep.isdigit():
                q = q.filter(
                    ~((MoveEval.game_id == int(eg)) & (MoveEval.ply == int(ep)))
                )
        me = q.order_by(solved.c.game_id.isnot(None), func.random()).first()
        if me is None:
            return jsonify(empty=True)
        payload = _puzzle_payload(me)
        if payload is None:
            return jsonify(empty=True)
        return jsonify(payload)

    @app.route("/api/puzzles/attempt", methods=["POST"])
    def record_puzzle_attempt():
        from models import PuzzleAttempt

        data = request.json or {}
        attempt = PuzzleAttempt(
            game_id=data.get("game_id"),
            ply=data.get("ply"),
            move_uci=(data.get("move_uci") or "")[:10],
            correct=bool(data.get("correct")),
        )
        db.session.add(attempt)
        db.session.commit()
        self_player = _self_player()
        return jsonify(ok=True, stats=_puzzle_stats(self_player.player_id) if self_player else None)

    # ── position search ──────────────────────────────────────────────────

    @app.route("/search/position")
    def position_search():
        """Find every library game that reached a given position, via the
        full-game Zobrist index (position_index.py). Candidates on the current
        page are verified by replaying their PGN to the matched ply and
        comparing epd() — a hash collision costs a false candidate, never a
        false result."""
        import chess as chess_lib
        import chess.pgn as chess_pgn
        import chess.polyglot as chess_polyglot
        import io as io_lib

        from models import PositionHash
        from position_index import _signed64

        fen = (request.args.get("fen") or "").strip()
        page = request.args.get("page", 1, type=int)
        board = None
        pagination = None
        results = []

        if fen:
            try:
                board = chess_lib.Board(fen)
            except ValueError:
                flash("That doesn't look like a valid FEN.", "error")
                board = None

        if board is not None:
            target_epd = board.epd()
            h = _signed64(chess_polyglot.zobrist_hash(board))
            matches = (
                db.session.query(
                    PositionHash.game_id, func.min(PositionHash.ply).label("ply")
                )
                .filter(PositionHash.zobrist == h)
                .group_by(PositionHash.game_id)
                .subquery()
            )
            pagination = (
                db.session.query(Game, matches.c.ply)
                .join(matches, matches.c.game_id == Game.game_id)
                .order_by(Game.played_at.desc())
                .paginate(page=page, per_page=50, error_out=False)
            )
            for game, ply in pagination.items:
                # collision guard: replay to the matched ply and verify
                try:
                    pgn_game = chess_pgn.read_game(io_lib.StringIO(game.pgn))
                    b = pgn_game.board()
                    for i, move in enumerate(pgn_game.mainline_moves(), start=1):
                        if i > ply:
                            break
                        b.push(move)
                    if b.epd() != target_epd:
                        continue
                except Exception:
                    continue
                results.append({"game": game, "ply": ply, "move_no": (ply + 1) // 2})

        return render_template(
            "position_search.html",
            fen=fen,
            valid=board is not None,
            results=results,
            pagination=pagination,
        )

    # ── manual PGN import ────────────────────────────────────────────────

    @app.route("/games/import")
    def import_page():
        return render_template("games_import.html")

    @app.route("/games/import", methods=["POST"])
    def import_games():
        from sync.manual import import_pgn_text

        text = request.form.get("pgn", "").strip()
        upload = request.files.get("pgn_file")
        if upload and upload.filename:
            text = (text + "\n\n" if text else "") + upload.read().decode(
                "utf-8", errors="replace"
            )
        if not text:
            flash("Paste a PGN or choose a file.", "error")
            return redirect(url_for("import_page"))

        result = import_pgn_text(text)
        if result["new_games"]:
            import opening_tree
            import position_index
            opening_tree.rebuild_all()
            position_index.rebuild_all()

        parts = [f"{result['new_games']} new game{'s' if result['new_games'] != 1 else ''}"]
        if result["duplicates"]:
            parts.append(f"{result['duplicates']} already imported")
        if result["skipped"]:
            parts.append(f"{result['skipped']} skipped (variants, empty, or unparseable)")
        flash("Imported: " + ", ".join(parts) + ".", "error" if not result["new_games"] else "message")
        if result["new_games"]:
            return redirect(url_for("games_list", source="manual"))
        return redirect(url_for("import_page"))

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
        import opening_tree
        import position_index
        click.echo(f"opening tree: {opening_tree.rebuild_all()}")
        click.echo(f"position index: {position_index.rebuild_all()}")

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
        import opening_tree
        import position_index
        click.echo(f"opening tree: {opening_tree.rebuild_all()}")
        click.echo(f"position index: {position_index.rebuild_all()}")

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
        import opening_tree
        import position_index
        click.echo(f"opening tree: {opening_tree.rebuild_all()}")
        click.echo(f"position index: {position_index.rebuild_all()}")

    @app.cli.command("sync-broadcasts")
    def cli_sync_broadcasts():
        """Sync the curated major-tournament broadcasts (broadcast_tournaments.py)."""
        import logging
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
        from sync.broadcasts import sync_all

        for result in sync_all():
            click.echo(result)
        import opening_tree
        import position_index
        click.echo(f"opening tree: {opening_tree.rebuild_all()}")
        click.echo(f"position index: {position_index.rebuild_all()}")

    @app.cli.command("build-opening-tree")
    def cli_build_opening_tree():
        """Backfill the opening-explorer position tree from stored PGN text
        (no engine involved — safe to run any time, skips games already
        ingested)."""
        import logging
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
        import opening_tree
        import position_index

        click.echo(f"opening tree: {opening_tree.rebuild_all()}")
        click.echo(f"position index: {position_index.rebuild_all()}")

    @app.cli.command("build-position-index")
    def cli_build_position_index():
        """Backfill the full-game Zobrist position index (position_index.py).
        Idempotent — only processes games not yet indexed."""
        import logging

        logging.basicConfig(level=logging.INFO)
        import position_index

        click.echo(f"position index: {position_index.rebuild_all()}")

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
