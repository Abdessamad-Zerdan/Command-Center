"""Shared local-filesystem helpers for the project registry: directory
walking with noise-directory exclusion, last-modified lookup, git
metadata, and the nested structure the file-tree accordion renders.

First subprocess/git shell-out in this codebase — no existing precedent
to match syntactically, so the try/except-and-degrade shape is carried
over from pipeline.py's per-source pattern instead: broad except,
log, return None rather than raise. A project with no git repo, a
moved/deleted path, or a missing `git` binary degrades just that one
block, never the page.

build_file_tree respects the project's own root .gitignore via
pathspec's real gitignore-pattern semantics (globs, negation, directory-only
entries) — not a hand-rolled subset. This is a security-relevant
exclusion (keeping something like credentials.json out of the tree
entirely), so correctness matters more than avoiding the dependency.
Nested per-directory .gitignore files aren't handled — only the root
one — a deliberate scope cut, not an oversight. walk_project_files/
last_modified_file (the "last worked on" signal) are untouched by this
— they use their own hardcoded runtime-extension exclusion instead,
scoped separately on purpose.
"""

import logging
import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

import pathspec

from command_center.config import TZ

logger = logging.getLogger(__name__)

_EXCLUDED_DIR_NAMES = {"node_modules", "__pycache__", "venv", "env", "dist", "build", "target", "coverage"}
# Runtime/generated artifacts that shouldn't win "most recently touched" —
# a live app's own database or log file gets written on every run, so by
# mtime alone it beats real source files almost every time. Hardcoded, not
# real .gitignore parsing (see module docstring) — a future consideration
# if this list proves insufficient, not committed to today.
_EXCLUDED_FILE_EXTENSIONS = {".db", ".log", ".sqlite", ".sqlite3"}
_MAX_TREE_ENTRIES = 2000


def _is_excluded_dir(name: str) -> bool:
    return name.startswith(".") or name in _EXCLUDED_DIR_NAMES


def _is_excluded_file(path: Path) -> bool:
    return path.suffix.lower() in _EXCLUDED_FILE_EXTENSIONS


# File-tree icon categories — weight (not hue) distinguishes config from
# doc/markdown, since both render the same icon shape in the template.
_CODE_EXTENSIONS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".rs", ".java", ".c", ".cpp",
    ".cs", ".rb", ".php", ".sh", ".sql", ".html", ".css", ".scss",
}
_DOC_EXTENSIONS = {".md", ".mdx", ".txt", ".rst"}
_CONFIG_EXTENSIONS = {".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".env"}


def _file_category(name: str) -> str | None:
    suffix = Path(name).suffix.lower()
    if suffix in _CODE_EXTENSIONS:
        return "code"
    if suffix in _CONFIG_EXTENSIONS:
        return "config"
    if suffix in _DOC_EXTENSIONS:
        return "doc"
    return None


def _load_gitignore_spec(root: Path) -> pathspec.PathSpec | None:
    """Parses the project's own root .gitignore (if present) with
    pathspec's gitignore-pattern semantics. None if there's no .gitignore or
    it can't be read — degrades to "nothing extra excluded," same
    posture as every other best-effort helper in this module."""
    gitignore_path = root / ".gitignore"
    if not gitignore_path.is_file():
        return None
    try:
        lines = gitignore_path.read_text(encoding="utf-8", errors="ignore").splitlines()
        return pathspec.PathSpec.from_lines("gitignore", lines)
    except OSError:
        logger.warning("Could not read .gitignore at %s", gitignore_path, exc_info=True)
        return None


def _is_gitignored(spec: pathspec.PathSpec | None, rel_path: Path, is_dir: bool) -> bool:
    if spec is None:
        return False
    rel_str = rel_path.as_posix() + ("/" if is_dir else "")
    return spec.match_file(rel_str)


def walk_project_files(root: Path) -> list[Path]:
    """Recursive walk, skipping noise dirs and runtime-artifact files.
    Returns absolute file paths. [] if root doesn't exist or isn't a
    readable directory — a project whose registered path moved or was
    deleted degrades to empty, not a crash."""
    if not root.is_dir():
        return []
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not _is_excluded_dir(d)]
        files.extend(
            Path(dirpath) / name for name in filenames if not _is_excluded_file(Path(name))
        )
    return files


def last_modified_file(root: Path) -> dict[str, Any] | None:
    """{'name': relative path, 'mtime': datetime} for the most recently
    modified file in the tree, or None if the dir is empty/missing."""
    files = walk_project_files(root)
    if not files:
        return None
    newest = max(files, key=lambda p: p.stat().st_mtime)
    return {
        "name": str(newest.relative_to(root)),
        "mtime": datetime.fromtimestamp(newest.stat().st_mtime, tz=TZ),
    }


def last_commit_info(root: Path) -> dict[str, Any] | None:
    """{'message': str, 'timestamp': datetime} via `git log -1`, or None
    if not a git repo / git missing / any failure. %cI (strict ISO 8601)
    rather than git's default %ci — guaranteed datetime.fromisoformat-
    parseable, avoiding the space-separated non-strict format's edge
    cases."""
    if not (root / ".git").is_dir():
        return None
    try:
        result = subprocess.run(
            ["git", "log", "-1", "--format=%s|%cI"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        message, _, timestamp = result.stdout.strip().partition("|")
        if not timestamp:
            return None
        return {"message": message, "timestamp": datetime.fromisoformat(timestamp)}
    except Exception:
        logger.warning("git log failed for %s", root, exc_info=True)
        return None


def build_file_tree(root: Path, max_entries: int = _MAX_TREE_ENTRIES) -> dict[str, Any] | None:
    """Nested {'name': str, 'type': 'dir'|'file', 'children': [...]}
    for accordion rendering. None if root doesn't exist or is empty.
    Capped at max_entries total nodes — a '...truncated' leaf is
    appended under the root once the cap is hit, so pathologically
    large repos don't produce an unbounded page.
    """
    if not root.is_dir():
        return None

    spec = _load_gitignore_spec(root)
    count = 0
    truncated = False

    def _build(dir_path: Path) -> list[dict[str, Any]]:
        nonlocal count, truncated
        children: list[dict[str, Any]] = []
        try:
            entries = sorted(dir_path.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        except OSError:
            return children
        for entry in entries:
            if truncated:
                break
            rel_path = entry.relative_to(root)
            if entry.is_dir():
                if _is_excluded_dir(entry.name) or _is_gitignored(spec, rel_path, is_dir=True):
                    continue
                count += 1
                if count > max_entries:
                    truncated = True
                    break
                children.append({"name": entry.name, "type": "dir", "children": _build(entry)})
            else:
                if _is_gitignored(spec, rel_path, is_dir=False):
                    continue
                count += 1
                if count > max_entries:
                    truncated = True
                    break
                children.append(
                    {"name": entry.name, "type": "file", "category": _file_category(entry.name)}
                )
        return children

    children = _build(root)
    if truncated:
        children.append({"name": "…truncated", "type": "file"})
    if not children:
        return None
    return {"name": root.name, "type": "dir", "children": children}


def count_files(node: dict[str, Any] | None) -> int:
    """Recursive file count for a build_file_tree() node — used for the
    collapsed file-tree disclosure's summary badge. Excludes the
    synthetic "…truncated" placeholder leaf, which isn't a real file."""
    if node is None:
        return 0
    total = 0
    for child in node.get("children", []):
        if child["type"] == "file":
            if child["name"] != "…truncated":
                total += 1
        else:
            total += count_files(child)
    return total
