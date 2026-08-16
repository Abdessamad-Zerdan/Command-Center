"""Server-rendered SVG charts. Colors are Tailwind utility classes (not
inline hex) so dark mode — a pure client-side localStorage toggle, see
base.html — applies to chart elements the same way it applies to every
other Tailwind-classed element on the page; the Tailwind CDN script scans
the full rendered DOM, including this raw SVG markup, not just template
source.
"""

from typing import Any

from command_center.config import LANE_LABELS, LANES

_LINE = "stroke-rust dark:stroke-rust-light"
_DOT = "fill-rust dark:fill-rust-light"
_MUTED_TEXT = "fill-stone-400 dark:fill-stone-500"
_MUTED_STROKE = "stroke-stone-300 dark:stroke-stone-600"
_BAR_CREATED = "fill-stone-300 dark:fill-stone-600"
_BAR_COMPLETED = "fill-rust dark:fill-rust-light"


def render_trend_svg(buckets: list[dict[str, Any]], width: int = 640, height: int = 160) -> str:
    pad_x, pad_y = 32, 20
    inner_w, inner_h = width - 2 * pad_x, height - 2 * pad_y
    n = len(buckets)
    values = [b["count"] for b in buckets]
    max_v = max(values) if values and max(values) > 0 else 1  # avoid /0 on an all-zero window

    def x_at(i: int) -> float:
        return pad_x if n <= 1 else pad_x + inner_w * i / (n - 1)

    def y_at(v: int) -> float:
        return pad_y + inner_h - inner_h * v / max_v

    points = [(x_at(i), y_at(b["count"])) for i, b in enumerate(buckets)]
    poly = " ".join(f"{x:.1f},{y:.1f}" for x, y in points)
    circles = "".join(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2.5" class="{_DOT}" />' for x, y in points)

    max_i = max(range(n), key=lambda i: values[i]) if values else 0
    label_idx = sorted({0, n - 1, max_i}) if n else []
    labels = "".join(
        f'<text x="{points[i][0]:.1f}" y="{height - 4}" font-size="9" text-anchor="middle" '
        f'class="{_MUTED_TEXT}">{buckets[i]["bucket"]}</text>'
        for i in label_idx
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


def render_lane_bars_svg(completion: dict[str, Any], width: int = 640, height: int = 220) -> str:
    pad_x, pad_y, label_h = 32, 16, 24
    inner_w, inner_h = width - 2 * pad_x, height - 2 * pad_y - label_h
    by_lane = completion["by_lane"]
    # LANES first (fixed, familiar order), then any bucket outside that
    # set (in practice just "unknown" — items with no recorded 'created'
    # event, e.g. pre-migration data) so the chart's bars always sum to
    # the same totals shown in the stat cards above it, never a silent
    # undercount.
    lanes = [l for l in LANES if l in by_lane] + sorted(l for l in by_lane if l not in LANES)
    max_v = max((max(by_lane[l]["created"], by_lane[l]["completed"]) for l in lanes), default=0) or 1

    group_w = inner_w / max(len(lanes), 1)
    bar_w, gap = group_w * 0.28, group_w * 0.08
    base_y = pad_y + inner_h

    bars, labels = [], []
    for i, lane in enumerate(lanes):
        group_x = pad_x + i * group_w
        created_h = inner_h * by_lane[lane]["created"] / max_v
        completed_h = inner_h * by_lane[lane]["completed"] / max_v
        created_x = group_x + group_w / 2 - bar_w - gap / 2
        completed_x = group_x + group_w / 2 + gap / 2
        bars.append(
            f'<rect x="{created_x:.1f}" y="{base_y - created_h:.1f}" width="{bar_w:.1f}" '
            f'height="{created_h:.1f}" class="{_BAR_CREATED}" />'
        )
        bars.append(
            f'<rect x="{completed_x:.1f}" y="{base_y - completed_h:.1f}" width="{bar_w:.1f}" '
            f'height="{completed_h:.1f}" class="{_BAR_COMPLETED}" />'
        )
        labels.append(
            f'<text x="{group_x + group_w / 2:.1f}" y="{height - 6}" font-size="9" '
            f'text-anchor="middle" class="{_MUTED_TEXT}">{LANE_LABELS.get(lane, lane)}</text>'
        )

    legend = (
        f'<rect x="{pad_x}" y="2" width="8" height="8" class="{_BAR_CREATED}" />'
        f'<text x="{pad_x + 12}" y="10" font-size="9" class="{_MUTED_TEXT}">Created</text>'
        f'<rect x="{pad_x + 70}" y="2" width="8" height="8" class="{_BAR_COMPLETED}" />'
        f'<text x="{pad_x + 82}" y="10" font-size="9" class="{_MUTED_TEXT}">Completed</text>'
    )
    return (
        f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" xmlns="http://www.w3.org/2000/svg">'
        f'{legend}{"".join(bars)}{"".join(labels)}'
        f"</svg>"
    )
