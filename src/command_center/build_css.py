"""Builds static/css/app.css with Tailwind 3.4.17 — once, instead of in the
browser on every page load (cdn.tailwindcss.com compiled the CSS at
runtime, blocking the first paint by about a second and restyling the page
after it showed).

    uv run make-css          # rebuild after changing templates
    uv run make-css --check  # CI: fail if app.css is out of date

Uses pytailwindcss (dev dependency), which runs Tailwind's official
standalone binary — no Node. The version is pinned here so a rebuild
never silently changes the look.
"""

import argparse
import sys
import tempfile
from pathlib import Path

TAILWIND_VERSION = "v3.4.17"
REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG = REPO_ROOT / "tailwind.config.js"
SOURCE = REPO_ROOT / "static" / "css" / "tailwind.src.css"
OUTPUT = REPO_ROOT / "static" / "css" / "app.css"


def build(output: Path = OUTPUT) -> None:
    import pytailwindcss  # dev-only dependency, imported only when building

    pytailwindcss.run(
        ["-c", str(CONFIG), "-i", str(SOURCE), "-o", str(output), "--minify"],
        cwd=REPO_ROOT,
        version=TAILWIND_VERSION,
        auto_install=True,
    )


def is_up_to_date() -> bool:
    with tempfile.TemporaryDirectory() as tmp:
        fresh = Path(tmp) / "app.css"
        build(fresh)
        # Line endings ignored: git on Windows may check app.css out as CRLF.
        return OUTPUT.exists() and _lf(fresh.read_bytes()) == _lf(OUTPUT.read_bytes())


def _lf(data: bytes) -> bytes:
    return data.replace(b"\r\n", b"\n")


def newer_than_css() -> list[Path]:
    """Templates changed after app.css was built — the app warns about
    these at startup (a cheap mtime check, no build; templates are where
    nearly all classes live, so Python edits don't trigger it). A missing
    app.css is returned as the one problem to report."""
    if not OUTPUT.exists():
        return [OUTPUT]
    built = OUTPUT.stat().st_mtime
    templates = (REPO_ROOT / "src" / "command_center" / "templates").rglob("*.html")
    return [p for p in templates if p.stat().st_mtime > built]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="fail if app.css is out of date")
    args = parser.parse_args(argv)
    if args.check:
        if is_up_to_date():
            print("static/css/app.css is up to date.")
            return 0
        print("static/css/app.css is out of date — run `uv run make-css` and commit it.", file=sys.stderr)
        return 1
    build()
    print(f"Built {OUTPUT.relative_to(REPO_ROOT)} ({OUTPUT.stat().st_size // 1024} KB).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
