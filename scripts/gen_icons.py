"""Regenerates the PWA icon PNGs in static/img/. Stdlib only (zlib for
PNG IDAT compression, struct for chunk packing) — no Pillow dependency
for what's just a flat-color badge with a few rectangle bars.

Usage: uv run python scripts/gen_icons.py static/img/icon-192.png static/img/icon-512.png static/img/favicon-32.png
"""
import struct
import sys
import zlib

RUST = (181, 86, 58)      # #b5563a
CREAM = (250, 246, 241)   # #faf6f1

_SIZES = [192, 512, 32]


def _chunk(tag: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))


def make_icon(size: int, path: str) -> None:
    pixels = [[RUST for _ in range(size)] for _ in range(size)]

    # Three horizontal bars, left-aligned, decreasing width — a simple
    # kanban/checklist mark echoing this app's own lane-column layout.
    bar_h = max(1, round(size * 0.09))
    gap = max(1, round(size * 0.07))
    pad_left = round(size * 0.22)
    widths = [0.56, 0.42, 0.28]
    total_h = bar_h * 3 + gap * 2
    top = (size - total_h) // 2

    for i, w_frac in enumerate(widths):
        y0 = top + i * (bar_h + gap)
        y1 = y0 + bar_h
        x0 = pad_left
        x1 = x0 + round(size * w_frac)
        for y in range(max(0, y0), min(size, y1)):
            for x in range(max(0, x0), min(size, x1)):
                pixels[y][x] = CREAM

    raw = bytearray()
    for row in pixels:
        raw.append(0)  # filter type: None
        for r, g, b in row:
            raw += bytes((r, g, b))

    idat = zlib.compress(bytes(raw), 9)
    png = (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
        + _chunk(b"IDAT", idat)
        + _chunk(b"IEND", b"")
    )
    with open(path, "wb") as f:
        f.write(png)


if __name__ == "__main__":
    for size, out_path in zip(_SIZES, sys.argv[1:]):
        make_icon(size, out_path)
        print(f"wrote {out_path} ({size}x{size})")
