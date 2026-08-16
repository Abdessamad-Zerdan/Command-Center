"""Server-rendered SVG charts for /finances — same technique as
history/charts.py (Tailwind utility classes baked into raw SVG, so the
client-side dark-mode toggle applies to chart elements for free, since
the Tailwind CDN script scans the full rendered DOM). Kept self-contained
here rather than importing history/charts.py's helpers: the shapes
differ (a single-series bar per category here, not lane's paired
created/completed bars), and each feature package owns its own chart
rendering, same as history/projects don't cross-import either.
"""

from typing import Any

from command_center.config import FINANCE_CATEGORIES

_LINE = "stroke-rust dark:stroke-rust-light"
_DOT = "fill-rust dark:fill-rust-light"
_MUTED_TEXT = "fill-stone-400 dark:fill-stone-500"
_MUTED_STROKE = "stroke-stone-300 dark:stroke-stone-600"
_BAR = "fill-rust dark:fill-rust-light"


def render_trend_svg(buckets: list[dict[str, Any]], width: int = 640, height: int = 160) -> str:
    pad_x, pad_y = 32, 20
    inner_w, inner_h = width - 2 * pad_x, height - 2 * pad_y
    n = len(buckets)
    values = [b["count"] for b in buckets]
    max_v = max(values) if values and max(values) > 0 else 1  # avoid /0 on an all-zero window

    def x_at(i: int) -> float:
        return pad_x if n <= 1 else pad_x + inner_w * i / (n - 1)

    def y_at(v: float) -> float:
        return pad_y + inner_h - inner_h * v / max_v

    points = [(x_at(i), y_at(b["count"])) for i, b in enumerate(buckets)]
    poly = " ".join(f"{x:.1f},{y:.1f}" for x, y in points)
    circles = "".join(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2.5" class="{_DOT}" />' for x, y in points)

    labels = "".join(
        f'<text x="{points[i][0]:.1f}" y="{height - 4}" font-size="9" text-anchor="middle" '
        f'class="{_MUTED_TEXT}">{buckets[i]["bucket"]}</text>'
        for i in range(n)
    )
    baseline_y = pad_y + inner_h
    return (
        f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" xmlns="http://www.w3.org/2000/svg">'
        f'<line x1="{pad_x}" y1="{baseline_y}" x2="{width - pad_x}" y2="{baseline_y}" '
        f'class="{_MUTED_STROKE}" stroke-width="1" opacity="0.4" />'
        f'<polyline points="{poly}" fill="none" class="{_LINE}" stroke-width="2" />'
        f"{circles}{labels}"
        f"</svg>"
    )


def render_category_bars_svg(breakdown: dict[str, float], width: int = 640, height: int = 220) -> str:
    pad_x, pad_y, label_h = 32, 16, 24
    inner_w, inner_h = width - 2 * pad_x, height - 2 * pad_y - label_h
    # FINANCE_CATEGORIES first (fixed, familiar order), then any bucket
    # outside that set, so a category renamed/removed from config later
    # doesn't silently drop older entries from the chart's total.
    categories = [c for c in FINANCE_CATEGORIES if c in breakdown] + sorted(
        c for c in breakdown if c not in FINANCE_CATEGORIES
    )
    max_v = max((breakdown[c] for c in categories), default=0) or 1

    group_w = inner_w / max(len(categories), 1)
    bar_w = group_w * 0.5
    base_y = pad_y + inner_h

    bars, labels = [], []
    for i, cat in enumerate(categories):
        group_x = pad_x + i * group_w
        bar_h = inner_h * breakdown[cat] / max_v
        bar_x = group_x + group_w / 2 - bar_w / 2
        bars.append(
            f'<rect x="{bar_x:.1f}" y="{base_y - bar_h:.1f}" width="{bar_w:.1f}" '
            f'height="{bar_h:.1f}" class="{_BAR}" />'
        )
        labels.append(
            f'<text x="{group_x + group_w / 2:.1f}" y="{height - 6}" font-size="9" '
            f'text-anchor="middle" class="{_MUTED_TEXT}">{cat}</text>'
        )
    return (
        f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" xmlns="http://www.w3.org/2000/svg">'
        f'{"".join(bars)}{"".join(labels)}'
        f"</svg>"
    )
