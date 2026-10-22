"""Prebuilt CSS instead of Tailwind's in-browser compiler, and how /static
is cached."""

import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from command_center import auth, build_css, db
from command_center.assistant import ingest as assistant_ingest
from command_center.setup_wizard import status as setup_status


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(auth, "has_valid_credentials", lambda: False)
    monkeypatch.setattr(setup_status, "is_setup_complete", lambda: True)
    monkeypatch.setattr(assistant_ingest, "rebuild_index", lambda *a, **k: False)
    from command_center.app import app

    with TestClient(app) as test_client:
        yield test_client


def test_pages_load_the_prebuilt_css_not_the_runtime_compiler(client: TestClient) -> None:
    html = client.get("/brief").text
    assert '<link rel="stylesheet" href="/static/css/app.css">' in html
    assert "cdn.tailwindcss.com" not in html


def test_the_prebuilt_css_is_committed_and_has_the_brand_colours() -> None:
    css = build_css.OUTPUT.read_text(encoding="utf-8")
    assert ".bg-rust{" in css and ".dark\\:bg-rust-light" in css  # theme + class-based dark mode carried over


def test_static_files_are_revalidated_not_redownloaded(client: TestClient) -> None:
    first = client.get("/static/css/app.css")
    assert first.status_code == 200
    assert first.headers["cache-control"] == "no-cache"

    again = client.get("/static/css/app.css", headers={"If-None-Match": first.headers["etag"]})
    assert again.status_code == 304


def test_startup_warning_names_templates_edited_after_the_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    templates = tmp_path / "src" / "command_center" / "templates"
    templates.mkdir(parents=True)
    css = tmp_path / "app.css"
    css.write_text("x")
    old, new = templates / "old.html", templates / "new.html"
    old.write_text("x")
    new.write_text("x")
    now = time.time()
    os.utime(old, (now - 100, now - 100))
    os.utime(css, (now - 50, now - 50))
    os.utime(new, (now, now))
    monkeypatch.setattr(build_css, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(build_css, "OUTPUT", css)

    assert build_css.newer_than_css() == [new]

    css.unlink()
    assert build_css.newer_than_css() == [css]  # missing CSS is the problem to report


def test_pages_prefetch_links_on_hover_but_never_downloads_or_the_wizard(client: TestClient) -> None:
    import json
    import re

    html = client.get("/brief").text
    rules = json.loads(re.search(r'<script type="speculationrules">(.*?)</script>', html, re.S).group(1))
    [rule] = rules["prefetch"]  # prefetch only — never prerender, which would run the page
    assert "prerender" not in rules
    assert rule["eagerness"] == "moderate"  # on hover / press, not every link on load
    excluded = json.dumps(rule["where"])
    for never in ("/settings/export*", "/step/*", "/attachments/*", "[download]"):
        assert never in excluded


def test_no_cross_document_view_transition() -> None:
    # Snapshotting the outgoing brief for the fade cost ~1 s per click.
    css = (build_css.REPO_ROOT / "static" / "css" / "base.css").read_text(encoding="utf-8")
    assert "@view-transition" not in css


def test_no_page_loads_anything_from_a_third_party() -> None:
    # Fonts, Alpine and the CSS are all served from /static: a CDN request
    # blocked or slowed every page load, and leaks each visit to a third
    # party. Links the user clicks (Gmail, Calendar, Medium...) are fine;
    # this checks only what a page loads by itself.
    import re

    templates = (build_css.REPO_ROOT / "src" / "command_center" / "templates").rglob("*.html")
    statics = [*(build_css.REPO_ROOT / "static" / "css").glob("*.css"), build_css.REPO_ROOT / "static" / "js" / "base.js"]
    loads = re.compile(r'(?:<script[^>]+src|<link[^>]+href|url\()\s*=?\s*["\']?(https?:)?//', re.I)
    offenders = [p.name for p in [*templates, *statics] if loads.search(p.read_text(encoding="utf-8"))]
    assert offenders == []
