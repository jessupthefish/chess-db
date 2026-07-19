"""
stats.py — SQL aggregation helpers behind the /stats insights hub.

Everything here is per-request live aggregation over the game table (the
project norm — no cached stats tables). All helpers take the self player's id
and an optional time_class filter. Queries lean on the existing indexes
(white_id/black_id/played_at/opening_id) and stay single-digit-ms at ~24k
games.

result_cases() is the one shared building block: the win/loss/draw case()
trio that used to be duplicated across _player_record, _opening_breakdown,
and _opening_tree_children in app.py.

The "analysis insights" helpers only ever join games that already have a
completed on-demand analysis (GameAnalysis.analyzed_at IS NOT NULL) — they
never trigger engine work, per the project's no-bulk-analysis rule.
"""

from sqlalchemy import Integer, and_, case, func

from models import Game, GameAnalysis, MoveEval, db

# ── shared building blocks ──────────────────────────────────────────────────


def result_cases(player_id):
    """(win, loss, draw) case() expressions, color-aware for one player."""
    win = case(
        (and_(Game.white_id == player_id, Game.result == "1-0"), 1),
        (and_(Game.black_id == player_id, Game.result == "0-1"), 1),
        else_=0,
    )
    loss = case(
        (and_(Game.white_id == player_id, Game.result == "0-1"), 1),
        (and_(Game.black_id == player_id, Game.result == "1-0"), 1),
        else_=0,
    )
    draw = case((Game.result == "1/2-1/2", 1), else_=0)
    return win, loss, draw


def _self_result_case(player_id):
    """'win' / 'loss' / 'draw' / NULL label for each game, from self's POV."""
    win, loss, draw = result_cases(player_id)
    return case((win == 1, "win"), (loss == 1, "loss"), (draw == 1, "draw"), else_=None)


def _filters(player_id, time_class=None):
    f = [(Game.white_id == player_id) | (Game.black_id == player_id)]
    if time_class:
        f.append(Game.time_class == time_class)
    return f


def _wld_query(player_id, group_cols, time_class=None, extra_filters=()):
    """GROUP BY the given columns, aggregating W/L/D + total."""
    win, loss, draw = result_cases(player_id)
    return (
        db.session.query(
            *group_cols,
            func.coalesce(func.sum(win), 0).label("wins"),
            func.coalesce(func.sum(loss), 0).label("losses"),
            func.coalesce(func.sum(draw), 0).label("draws"),
            func.count().label("total"),
        )
        .filter(*_filters(player_id, time_class))
        .filter(*extra_filters)
        .group_by(*group_cols)
    )


def score_pct(wins, draws, total):
    """Chess score percentage: wins count 1, draws half."""
    if not total:
        return None
    return round((wins + 0.5 * draws) / total * 100, 1)


def available_time_classes(player_id):
    rows = (
        db.session.query(Game.time_class, func.count())
        .filter(*_filters(player_id))
        .filter(Game.time_class.isnot(None))
        .group_by(Game.time_class)
        .order_by(func.count().desc())
        .all()
    )
    return [tc for tc, _ in rows]


# ── result breakdowns ───────────────────────────────────────────────────────


def results_by_color(player_id, time_class=None):
    """[{color, wins, losses, draws, total, score}] for white and black."""
    color = case((Game.white_id == player_id, "white"), else_="black")
    rows = _wld_query(player_id, [color.label("color")], time_class).all()
    by_color = {r.color: r for r in rows}
    out = []
    for c in ("white", "black"):
        r = by_color.get(c)
        out.append({
            "color": c,
            "wins": r.wins if r else 0,
            "losses": r.losses if r else 0,
            "draws": r.draws if r else 0,
            "total": r.total if r else 0,
            "score": score_pct(r.wins, r.draws, r.total) if r else None,
        })
    return out


def results_by_time_class(player_id):
    rows = (
        _wld_query(player_id, [Game.time_class])
        .order_by(func.count().desc())
        .all()
    )
    return [
        {
            "time_class": r.time_class or "unknown",
            "wins": r.wins, "losses": r.losses, "draws": r.draws,
            "total": r.total, "score": score_pct(r.wins, r.draws, r.total),
        }
        for r in rows
    ]


# how a termination value reads from the winner's / loser's side
_TERMINATION_LABELS = {
    "checkmated": "checkmate",
    "resigned": "resignation",
    "timeout": "timeout",
    "abandoned": "abandonment",
    "agreed": "agreement",
    "repetition": "repetition",
    "stalemate": "stalemate",
    "insufficient": "insufficient material",
    "timevsinsufficient": "timeout vs insufficient",
    "50move": "50-move rule",
    "": "unknown",
    None: "unknown",
}


def results_by_termination(player_id, time_class=None):
    """How you win vs how you lose: termination counts split by self-result."""
    self_result = _self_result_case(player_id)
    rows = (
        db.session.query(self_result.label("res"), Game.termination, func.count())
        .filter(*_filters(player_id, time_class))
        .group_by("res", Game.termination)
        .all()
    )
    wins, losses, draws = {}, {}, {}
    for res, term, count in rows:
        label = _TERMINATION_LABELS.get(term, term)
        bucket = {"win": wins, "loss": losses, "draw": draws}.get(res)
        if bucket is not None:
            bucket[label] = bucket.get(label, 0) + count
    sort = lambda d: sorted(d.items(), key=lambda kv: -kv[1])
    return {"wins": sort(wins), "losses": sort(losses), "draws": sort(draws)}


# ── when you play / when you win ────────────────────────────────────────────

_DOW = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]


def results_by_dow(player_id, time_class=None):
    """Score% per day of week (server-local time). Mon-first ordering."""
    dow = func.strftime("%w", Game.played_at, "localtime")
    rows = _wld_query(player_id, [dow.label("dow")], time_class,
                      extra_filters=[Game.played_at.isnot(None)]).all()
    by_dow = {int(r.dow): r for r in rows if r.dow is not None}
    out = []
    for d in (1, 2, 3, 4, 5, 6, 0):  # Monday-first
        r = by_dow.get(d)
        out.append({
            "label": _DOW[d],
            "total": r.total if r else 0,
            "score": score_pct(r.wins, r.draws, r.total) if r else None,
        })
    return out


def results_by_hour(player_id, time_class=None):
    """Score% per local hour of day, 24 buckets."""
    hour = func.strftime("%H", Game.played_at, "localtime")
    rows = _wld_query(player_id, [hour.label("hour")], time_class,
                      extra_filters=[Game.played_at.isnot(None)]).all()
    by_hour = {int(r.hour): r for r in rows if r.hour is not None}
    out = []
    for h in range(24):
        r = by_hour.get(h)
        out.append({
            "label": f"{h:02d}",
            "total": r.total if r else 0,
            "score": score_pct(r.wins, r.draws, r.total) if r else None,
        })
    return out


def monthly_activity(player_id, time_class=None):
    """Games per month as {year: [12 cells]}, newest year first, plus the max
    monthly count (for heatmap intensity scaling)."""
    month = func.strftime("%Y-%m", Game.played_at, "localtime")
    rows = _wld_query(player_id, [month.label("ym")], time_class,
                      extra_filters=[Game.played_at.isnot(None)]).all()
    years = {}
    max_count = 0
    for r in rows:
        if not r.ym:
            continue
        y, m = r.ym.split("-")
        cells = years.setdefault(int(y), [None] * 12)
        cells[int(m) - 1] = {
            "count": r.total, "wins": r.wins, "losses": r.losses,
            "draws": r.draws, "score": score_pct(r.wins, r.draws, r.total),
        }
        max_count = max(max_count, r.total)
    return dict(sorted(years.items(), reverse=True)), max_count


# ── rating history ──────────────────────────────────────────────────────────


def rating_history(player_id, time_class=None, max_points=200):
    """Full-history rating lines, one per time_class: last rating of each
    active day, downsampled evenly to <= max_points so the SVG stays small."""
    rating = case((Game.white_id == player_id, Game.white_rating), else_=Game.black_rating)
    rows = (
        db.session.query(Game.played_at, Game.time_class, rating.label("rating"))
        .filter(*_filters(player_id, time_class))
        .filter(rating.isnot(None), Game.played_at.isnot(None))
        .order_by(Game.played_at.asc())
        .all()
    )
    per_day = {}  # time_class -> {date: rating} (dict keeps last-write-wins)
    for played_at, tc, r in rows:
        per_day.setdefault(tc or "unknown", {})[played_at.date()] = r

    out = {}
    for tc, days in per_day.items():
        points = [(d.isoformat(), r) for d, r in sorted(days.items())]
        if len(points) > max_points:
            stride = len(points) / max_points
            sampled = [points[int(i * stride)] for i in range(max_points)]
            if sampled[-1] != points[-1]:
                sampled.append(points[-1])  # always keep the latest rating
            points = sampled
        if len(points) >= 2:
            out[tc] = points
    return dict(sorted(out.items(), key=lambda kv: -len(kv[1])))


# ── streaks ─────────────────────────────────────────────────────────────────


def streaks(player_id, time_class=None):
    """Current streak plus best win streak / worst loss streak, from the full
    ordered result sequence (one narrow SQL row per game, Python pass)."""
    self_result = _self_result_case(player_id)
    rows = (
        db.session.query(self_result)
        .filter(*_filters(player_id, time_class))
        .filter(Game.played_at.isnot(None))
        .order_by(Game.played_at.asc())
        .all()
    )
    best_win = worst_loss = 0
    run_type, run_len = None, 0
    for (res,) in rows:
        if res is None:
            continue
        if res == run_type:
            run_len += 1
        else:
            run_type, run_len = res, 1
        if run_type == "win":
            best_win = max(best_win, run_len)
        elif run_type == "loss":
            worst_loss = max(worst_loss, run_len)
    return {
        "current_type": run_type,
        "current_len": run_len,
        "best_win": best_win,
        "worst_loss": worst_loss,
    }


# ── opposition strength & game length ───────────────────────────────────────


def vs_rating_bands(player_id, time_class=None, band=100, min_games=5):
    """Score% against opponents bucketed by rating band."""
    opp_rating = case((Game.white_id == player_id, Game.black_rating), else_=Game.white_rating)
    band_expr = (func.cast(opp_rating / band, Integer) * band).label("band")
    rows = (
        _wld_query(player_id, [band_expr], time_class,
                   extra_filters=[opp_rating.isnot(None)])
        .order_by(band_expr.asc())
        .all()
    )
    return [
        {
            "band": r.band,
            "label": f"{r.band}",
            "total": r.total,
            "score": score_pct(r.wins, r.draws, r.total),
        }
        for r in rows
        if r.total >= min_games
    ]


def game_length_distribution(player_id, time_class=None, bucket=10, cap=80):
    """Game counts by length in full moves, 10-move buckets, capped at 80+."""
    moves = func.cast(Game.ply_count / 2, Integer)
    bucket_expr = func.min(func.cast(moves / bucket, Integer) * bucket, cap).label("bucket")
    rows = (
        _wld_query(player_id, [bucket_expr], time_class,
                   extra_filters=[Game.ply_count.isnot(None), Game.ply_count > 0])
        .order_by(bucket_expr.asc())
        .all()
    )
    return [
        {
            "label": f"{r.bucket}+" if r.bucket >= cap else f"{r.bucket}–{r.bucket + bucket - 1}",
            "total": r.total,
            "score": score_pct(r.wins, r.draws, r.total),
        }
        for r in rows
    ]


# ── analysis insights (analyzed games only — never triggers engine work) ───


def _analyzed_filters(player_id, time_class=None):
    return _filters(player_id, time_class) + [GameAnalysis.analyzed_at.isnot(None)]


def _self_mover_parity(player_id):
    """MoveEval rows where self made the move. MoveEval.ply is the move number
    (1..N): odd ply => White moved, even ply => Black moved."""
    return (
        ((MoveEval.ply % 2 == 1) & (Game.white_id == player_id))
        | ((MoveEval.ply % 2 == 0) & (Game.black_id == player_id))
    )


def analysis_insights(player_id, time_class=None):
    """ACPL + blunder-rate stats over the subset of self games that have a
    completed on-demand analysis. Returns None when no games are analyzed."""
    self_acpl = case(
        (Game.white_id == player_id, GameAnalysis.white_acpl),
        else_=GameAnalysis.black_acpl,
    )
    base = (
        db.session.query(func.count(), func.avg(self_acpl))
        .select_from(GameAnalysis)
        .join(Game, Game.game_id == GameAnalysis.game_id)
        .filter(*_analyzed_filters(player_id, time_class))
    )
    n_analyzed, avg_acpl = base.one()
    if not n_analyzed:
        return None

    acpl_rows = (
        db.session.query(Game.played_at, self_acpl)
        .select_from(GameAnalysis)
        .join(Game, Game.game_id == GameAnalysis.game_id)
        .filter(*_analyzed_filters(player_id, time_class))
        .filter(self_acpl.isnot(None), Game.played_at.isnot(None))
        .order_by(Game.played_at.asc())
        .all()
    )
    acpl_trend = [(p.date().isoformat(), round(a, 1)) for p, a in acpl_rows]

    # blunder/mistake rate per game phase, over self moves only
    phase = case(
        (MoveEval.ply <= 20, "opening"),
        (MoveEval.ply <= 60, "middlegame"),
        else_="endgame",
    )
    bad_move = case((MoveEval.classification.in_(["blunder", "mistake"]), 1), else_=0)
    blunder_only = case((MoveEval.classification == "blunder", 1), else_=0)
    phase_rows = (
        db.session.query(
            phase.label("phase"),
            func.count().label("moves"),
            func.coalesce(func.sum(bad_move), 0).label("bad"),
            func.coalesce(func.sum(blunder_only), 0).label("blunders"),
        )
        .select_from(MoveEval)
        .join(Game, Game.game_id == MoveEval.game_id)
        .join(GameAnalysis, GameAnalysis.game_id == Game.game_id)
        .filter(*_analyzed_filters(player_id, time_class))
        .filter(_self_mover_parity(player_id))
        .group_by("phase")
        .all()
    )
    order = {"opening": 0, "middlegame": 1, "endgame": 2}
    by_phase = [
        {
            "phase": r.phase,
            "moves": r.moves,
            "bad_rate": round(r.bad / r.moves * 100, 1) if r.moves else 0,
            "blunders": r.blunders,
        }
        for r in sorted(phase_rows, key=lambda r: order[r.phase])
    ]

    return {
        "n_analyzed": n_analyzed,
        "avg_acpl": round(avg_acpl, 1) if avg_acpl is not None else None,
        "acpl_trend": acpl_trend,
        "by_phase": by_phase,
    }
