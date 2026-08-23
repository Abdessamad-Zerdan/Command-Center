"""Server-rendered SVG charts for /fitness — same technique as
finances/charts.py (Tailwind utility classes baked into raw SVG, so the
client-side dark-mode toggle applies to chart elements for free). Kept
self-contained here rather than importing finances/charts.py's helpers,
same "each feature package owns its own chart rendering" reasoning that
module's own docstring gives.
"""

from typing import Any

_LINE = "stroke-rust dark:stroke-rust-light"
_DOT = "fill-rust dark:fill-rust-light"
_MUTED_TEXT = "fill-stone-400 dark:fill-stone-500"
_BAR = "fill-rust dark:fill-rust-light"
_BAND = "fill-emerald-500/10 dark:fill-emerald-400/10"
_CAP_LINE = "stroke-amber-500 dark:stroke-amber-400"


def render_weight_trend_svg(
    points: list[dict[str, Any]],
    goal_low: float,
    goal_high: float,
    width: int = 640,
    height: int = 200,
) -> str:
    """points: [{'log_date': 'YYYY-MM-DD', 'bodyweight_kg': float}, ...],
    oldest first (queries.list_bodyweight_log()'s own order). The goal
    band is always drawn, even with zero or one points logged, so the
    target zone is visible from day one."""
    pad_x, pad_y = 32, 20
    inner_w, inner_h = width - 2 * pad_x, height - 2 * pad_y
    n = len(points)

    band_min, band_max = min(goal_low, goal_high), max(goal_low, goal_high)
    values = [p["bodyweight_kg"] for p in points] + [band_min, band_max]
    min_v, max_v = min(values), max(values)
    span = (max_v - min_v) or 1  # avoid /0 when everything's equal
    # Breathing room so the line/band never touches the chart edge.
    min_v -= span * 0.1
    max_v += span * 0.1
    span = max_v - min_v

    def x_at(i: int) -> float:
        return pad_x if n <= 1 else pad_x + inner_w * i / (n - 1)

    def y_at(v: float) -> float:
        return pad_y + inner_h - inner_h * (v - min_v) / span

    band_y_top, band_y_bottom = y_at(band_max), y_at(band_min)
    band_rect = (
        f'<rect x="{pad_x}" y="{band_y_top:.1f}" width="{inner_w}" '
        f'height="{(band_y_bottom - band_y_top):.1f}" class="{_BAND}" />'
    )

    if n == 0:
        return (
            f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" xmlns="http://www.w3.org/2000/svg">'
            f"{band_rect}"
            f'<text x="{width / 2}" y="{height / 2}" font-size="11" text-anchor="middle" class="{_MUTED_TEXT}">'
            f"No weight logged yet</text>"
            f"</svg>"
        )

    pts = [(x_at(i), y_at(p["bodyweight_kg"])) for i, p in enumerate(points)]
    poly = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    circles = "".join(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2.5" class="{_DOT}" />' for x, y in pts)

    # Label only the first/last point — a dense daily x-axis would be
    # unreadable once there are more than a handful of entries.
    label_idxs = {0, n - 1} if n > 1 else {0}
    labels = "".join(
        f'<text x="{pts[i][0]:.1f}" y="{height - 4}" font-size="9" '
        f'text-anchor="{"start" if i == 0 else "end"}" class="{_MUTED_TEXT}">{points[i]["log_date"]}</text>'
        for i in sorted(label_idxs)
    )
    return (
        f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" xmlns="http://www.w3.org/2000/svg">'
        f"{band_rect}"
        f'<polyline points="{poly}" fill="none" class="{_LINE}" stroke-width="2" />'
        f"{circles}{labels}"
        f"</svg>"
    )


def render_bars_svg(
    buckets: list[dict[str, Any]],
    cap_value: float | None = None,
    width: int = 640,
    height: int = 200,
) -> str:
    """Generic single-series bar chart, shared by the weekly resistance-
    volume and weekly-running-distance charts. Draws a dashed reference
    line at cap_value when given (the running cap)."""
    pad_x, pad_y, label_h = 32, 16, 20
    inner_w, inner_h = width - 2 * pad_x, height - 2 * pad_y - label_h
    n = len(buckets)
    candidates = [b["count"] for b in buckets] + ([cap_value] if cap_value else [])
    max_v = max(candidates, default=0)
    max_v = max_v * 1.1 if max_v > 0 else 1  # headroom above the tallest bar/cap line

    group_w = inner_w / max(n, 1)
    bar_w = group_w * 0.55
    base_y = pad_y + inner_h

    bars, labels = [], []
    for i, b in enumerate(buckets):
        group_x = pad_x + i * group_w
        bar_h = inner_h * b["count"] / max_v
        bar_x = group_x + group_w / 2 - bar_w / 2
        bars.append(
            f'<rect x="{bar_x:.1f}" y="{base_y - bar_h:.1f}" width="{bar_w:.1f}" '
            f'height="{bar_h:.1f}" rx="1.5" class="{_BAR}" />'
        )
        labels.append(
            f'<text x="{group_x + group_w / 2:.1f}" y="{height - 4}" font-size="9" '
            f'text-anchor="middle" class="{_MUTED_TEXT}">{b["bucket"]}</text>'
        )

    cap_line = ""
    if cap_value:
        cap_y = base_y - inner_h * cap_value / max_v
        cap_line = (
            f'<line x1="{pad_x}" y1="{cap_y:.1f}" x2="{width - pad_x}" y2="{cap_y:.1f}" '
            f'class="{_CAP_LINE}" stroke-width="1.5" stroke-dasharray="4 3" />'
        )

    return (
        f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" xmlns="http://www.w3.org/2000/svg">'
        f'{"".join(bars)}{cap_line}{"".join(labels)}'
        f"</svg>"
    )
