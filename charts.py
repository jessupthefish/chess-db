"""
charts.py — server-rendered inline-SVG chart primitives for the stats pages.

Same approach as the dashboard sparkline (which lives here now as
sparkline_svg): all colors are CSS custom properties (var(--accent),
var(--border), var(--text2), var(--bg2)) so charts theme with the page's
dark/light mode automatically, with zero JS and zero dependencies.

Mark specs follow the dataviz skill: 2px lines with round joins, bars capped
at 24px with a 4px rounded data-end and a square baseline, hairline solid
gridlines in a recessive gray, >=8px end markers with a 2px surface ring,
selective direct labels (never a number on every point), text in text tokens
never in the series color, native <title> tooltips as the hover layer.
"""

from html import escape


def _nice_ticks(lo, hi, n=4):
    """Round tick values covering [lo, hi] — clean steps of 1/2/2.5/5 x 10^k."""
    if hi <= lo:
        hi = lo + 1
    span = hi - lo
    raw_step = span / max(n, 1)
    magnitude = 10 ** len(str(int(raw_step))) / 10 if raw_step >= 1 else 1
    step = magnitude
    for mult in (1, 2, 2.5, 5, 10):
        if magnitude * mult >= raw_step:
            step = magnitude * mult
            break
    first = int(lo // step) * step
    ticks = []
    t = first
    while t <= hi + step * 0.01:
        if t >= lo - step * 0.01:
            ticks.append(int(t) if float(t).is_integer() else t)
        t += step
    return ticks


def _fmt(v):
    if isinstance(v, float) and not v.is_integer():
        return f"{v:g}"
    return f"{int(v):,}"


def sparkline_svg(points, width=220, height=56, pad=8):
    """Compact single-series trend sparkline (moved verbatim from app.py).

    points: list of (label, value) in chronological order. 2px line, >=8px
    end-marker with a surface-color ring, direct end-label — no gridlines or
    legend; this is a stat-card sparkline, not a full chart.
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
    <title>{escape(str(last_label))}: {last_value}</title>
  </circle>
  <text x="{min(last_x + 8, width - 4)}" y="{last_y + 4:.1f}" text-anchor="{'end' if last_x + 8 > width - 30 else 'start'}" class="sparkline-label">{last_value}</text>
</svg>'''


def line_chart_svg(points, width=720, height=240, aria_label=""):
    """Full-size single-series line chart: y gridlines with rounded tick
    labels, first/last x date labels, area wash at 10% opacity, end marker
    with surface ring and a direct end-label, per-point <title> tooltips.

    points: [(label, value), ...] chronological. Single series — no legend
    (the surrounding card's heading names it, per the dataviz skill).
    """
    if len(points) < 2:
        return ""
    pad_l, pad_r, pad_t, pad_b = 44, 52, 10, 22
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b

    values = [v for _, v in points]
    lo, hi = min(values), max(values)
    if lo == hi:
        lo, hi = lo - 1, hi + 1
    # a little breathing room so the line doesn't kiss the frame
    span = hi - lo
    lo -= span * 0.05
    hi += span * 0.05
    ticks = _nice_ticks(lo, hi)

    def x_at(i):
        return pad_l + i / (len(points) - 1) * plot_w

    def y_at(v):
        return pad_t + (1 - (v - lo) / (hi - lo)) * plot_h

    coords = [(x_at(i), y_at(v)) for i, (_, v) in enumerate(points)]
    poly = " ".join(f"{x:.1f},{y:.1f}" for x, y in coords)
    baseline_y = pad_t + plot_h
    area = f"{pad_l:.1f},{baseline_y:.1f} {poly} {coords[-1][0]:.1f},{baseline_y:.1f}"

    grid = "".join(
        f'<line x1="{pad_l}" y1="{y_at(t):.1f}" x2="{pad_l + plot_w}" y2="{y_at(t):.1f}" class="chart-grid" />'
        f'<text x="{pad_l - 6}" y="{y_at(t) + 3.5:.1f}" text-anchor="end" class="chart-tick">{_fmt(t)}</text>'
        for t in ticks
        if lo <= t <= hi
    )

    # invisible hover targets, one per point, carrying native tooltips
    hovers = "".join(
        f'<circle cx="{x:.1f}" cy="{y:.1f}" r="7" fill="transparent"><title>{escape(str(label))}: {_fmt(value)}</title></circle>'
        for (x, y), (label, value) in zip(coords, points)
    )

    first_label, _ = points[0]
    last_label, last_value = points[-1]
    last_x, last_y = coords[-1]

    return f'''<svg viewBox="0 0 {width} {height}" class="chart" role="img" aria-label="{escape(aria_label)}">
  {grid}
  <polygon points="{area}" fill="var(--accent)" opacity="0.1" />
  <polyline points="{poly}" fill="none" stroke="var(--accent)" stroke-width="2" stroke-linejoin="round" stroke-linecap="round" />
  {hovers}
  <circle cx="{last_x:.1f}" cy="{last_y:.1f}" r="6" fill="var(--bg2)" />
  <circle cx="{last_x:.1f}" cy="{last_y:.1f}" r="4" fill="var(--accent)"><title>{escape(str(last_label))}: {_fmt(last_value)}</title></circle>
  <text x="{last_x + 8:.1f}" y="{last_y + 4:.1f}" class="chart-label">{_fmt(last_value)}</text>
  <text x="{pad_l}" y="{height - 6}" class="chart-tick">{escape(str(first_label))}</text>
  <text x="{pad_l + plot_w}" y="{height - 6}" text-anchor="end" class="chart-tick">{escape(str(last_label))}</text>
</svg>'''


def _top_rounded_bar(x, y, w, h, r=4):
    """Bar path with a 4px rounded data-end and a square baseline."""
    if h <= r:
        return f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{max(h, 1):.1f}" />'
    return (
        f'<path d="M{x:.1f},{y + h:.1f} v{-(h - r):.1f} q0,-{r} {r},-{r} '
        f'h{w - 2 * r:.1f} q{r},0 {r},{r} v{h - r:.1f} z" />'
    )


def bar_chart_svg(items, width=720, height=220, ref_value=None, ref_label=None,
                  y_max=None, percent=False, aria_label=""):
    """Column chart. items: [(x_label, value, tooltip), ...].

    Columns are capped at 24px wide with a 2px surface gap between adjacent
    bars, 4px rounded data-end, square baseline. Optional horizontal reference
    line (e.g. 50% score line). Direct value labels only when there are few
    bars (selective labeling); tooltips carry the rest.
    """
    if not items:
        return ""
    pad_l, pad_r, pad_t, pad_b = 40, 8, 14, 22
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b

    values = [v for _, v, _ in items]
    hi = y_max if y_max is not None else max(values + [1])
    if percent:
        hi = max(hi, 100 if hi > 60 else 60)
    ticks = _nice_ticks(0, hi)

    def y_at(v):
        return pad_t + (1 - v / hi) * plot_h

    slot = plot_w / len(items)
    bar_w = min(24, max(slot - 2, 2))  # 2px surface gap between neighbors

    grid = "".join(
        f'<line x1="{pad_l}" y1="{y_at(t):.1f}" x2="{pad_l + plot_w}" y2="{y_at(t):.1f}" class="chart-grid" />'
        f'<text x="{pad_l - 6}" y="{y_at(t) + 3.5:.1f}" text-anchor="end" class="chart-tick">{_fmt(t)}{"%" if percent else ""}</text>'
        for t in ticks
        if 0 <= t <= hi
    )

    ref = ""
    if ref_value is not None and 0 <= ref_value <= hi:
        ry = y_at(ref_value)
        ref = (
            f'<line x1="{pad_l}" y1="{ry:.1f}" x2="{pad_l + plot_w}" y2="{ry:.1f}" class="chart-ref" />'
            f'<text x="{pad_l + plot_w}" y="{ry - 4:.1f}" text-anchor="end" class="chart-tick">{escape(ref_label or _fmt(ref_value))}</text>'
        )

    label_tips = len(items) <= 12  # selective: skip per-bar values on dense charts
    bars, x_labels, tip_labels = [], [], []
    for i, (label, value, tooltip) in enumerate(items):
        cx = pad_l + slot * i + slot / 2
        bx = cx - bar_w / 2
        by = y_at(value)
        bh = pad_t + plot_h - by
        bars.append(
            f'<g class="chart-bar">{_top_rounded_bar(bx, by, bar_w, bh)}'
            f'<rect x="{pad_l + slot * i:.1f}" y="{pad_t}" width="{slot:.1f}" height="{plot_h}" fill="transparent">'
            f'<title>{escape(str(tooltip))}</title></rect></g>'
        )
        # x labels: thin out when crowded (every 3rd for 24 hourly bars)
        stride = 1 if len(items) <= 12 else 3
        if i % stride == 0:
            x_labels.append(
                f'<text x="{cx:.1f}" y="{height - 6}" text-anchor="middle" class="chart-tick">{escape(str(label))}</text>'
            )
        if label_tips and value > 0:
            tip_labels.append(
                f'<text x="{cx:.1f}" y="{by - 5:.1f}" text-anchor="middle" class="chart-label">{_fmt(value)}{"%" if percent else ""}</text>'
            )

    return f'''<svg viewBox="0 0 {width} {height}" class="chart" role="img" aria-label="{escape(aria_label)}">
  {grid}
  {"".join(bars)}
  {ref}
  {"".join(tip_labels)}
  {"".join(x_labels)}
</svg>'''
