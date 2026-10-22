"""Medium personal feed: unofficial RapidAPI (medium2.p.rapidapi.com).

Explicitly less reliable than the Google-backed sources — every network
call here is independently try/excepted, and pipeline.py wraps the whole
fetch_and_rank() call too, so a failure here only degrades the Reading
lane, never the rest of the brief.
"""

import json
import logging

import httpx

from command_center.config import MEDIUM_API_KEY, MEDIUM_USER_ID_CACHE, MEDIUM_USERNAME
from command_center.triage import TriageProviderError

logger = logging.getLogger(__name__)

BASE_URL = "https://medium2.p.rapidapi.com"
# What one Medium call can fail with in normal operation: transport or
# HTTP-status errors, and a body that isn't JSON (JSONDecodeError is a
# ValueError). Anything else is a bug in this module and should surface
# — pipeline._fetch_medium still contains it to the Reading lane.
_MEDIUM_ERRORS = (httpx.HTTPError, ValueError)
ARTICLE_LIMIT = 10
RANK_COUNT = 3


def _headers() -> dict:
    return {
        "x-rapidapi-key": MEDIUM_API_KEY,
        "x-rapidapi-host": "medium2.p.rapidapi.com",
    }


def _resolve_user_id(client: httpx.Client) -> str:
    """Cached on disk — only calls the "Get User ID" endpoint when the
    cache is missing or was written for a different MEDIUM_USERNAME.
    """
    if MEDIUM_USER_ID_CACHE.exists():
        cached = json.loads(MEDIUM_USER_ID_CACHE.read_text(encoding="utf-8"))
        if cached.get("username") == MEDIUM_USERNAME:
            return cached["user_id"]

    resp = client.get(f"{BASE_URL}/user/id_for/{MEDIUM_USERNAME}", headers=_headers())
    resp.raise_for_status()
    user_id = resp.json()["id"]
    MEDIUM_USER_ID_CACHE.write_text(
        json.dumps({"username": MEDIUM_USERNAME, "user_id": user_id}), encoding="utf-8"
    )
    return user_id


def _fetch_interests(client: httpx.Client, user_id: str) -> list[str]:
    try:
        resp = client.get(f"{BASE_URL}/user/{user_id}/interests", headers=_headers())
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else data.get("tags", [])
    except _MEDIUM_ERRORS:
        logger.exception("Medium interests fetch failed, skipping")
        return []


def _fetch_following(client: httpx.Client, user_id: str) -> list[str]:
    try:
        resp = client.get(f"{BASE_URL}/user/{user_id}/following", headers=_headers())
        resp.raise_for_status()
        data = resp.json()
        return data.get("following", []) if isinstance(data, dict) else data
    except _MEDIUM_ERRORS:
        logger.exception("Medium following fetch failed, skipping")
        return []


def _fetch_articles(client: httpx.Client, user_id: str) -> list[dict]:
    """Latest/recommended article ids for the user, resolved to summaries."""
    try:
        resp = client.get(f"{BASE_URL}/user/{user_id}/articles", headers=_headers())
        resp.raise_for_status()
        data = resp.json()
        article_ids = data.get("associated_articles", data) if isinstance(data, dict) else data
        if not isinstance(article_ids, list):
            return []
    except _MEDIUM_ERRORS:
        logger.exception("Medium articles list fetch failed, skipping")
        return []

    articles = []
    for article_id in article_ids[:ARTICLE_LIMIT]:
        try:
            resp = client.get(f"{BASE_URL}/article/{article_id}", headers=_headers())
            resp.raise_for_status()
            data = resp.json()
            articles.append(
                {
                    "id": article_id,
                    "title": data.get("title") or "(untitled)",
                    "subtitle": data.get("subtitle", ""),
                    "url": data.get("url", ""),
                }
            )
        except _MEDIUM_ERRORS:
            logger.exception("Medium article detail fetch failed for %s, skipping it", article_id)
    return articles


def _rank_articles(articles: list[dict], interests: list[str]) -> list[dict]:
    raise TriageProviderError("_rank_articles: withheld from the public showcase - see README.md")


def fetch_and_rank() -> list[dict]:
    """Returns dicts shaped like `items` table rows, lane='reading',
    source='medium' — dedup happens for free via the existing
    UNIQUE(source, source_id) on items, same as every other source.
    """
    if not MEDIUM_API_KEY or not MEDIUM_USERNAME:
        raise TriageProviderError("Medium feed needs MEDIUM_API_KEY and MEDIUM_USERNAME in .env.")

    with httpx.Client(timeout=15) as client:
        user_id = _resolve_user_id(client)
        interests = _fetch_interests(client, user_id)
        _fetch_following(client, user_id)
        articles = _fetch_articles(client, user_id)

    ranked = _rank_articles(articles, interests)

    return [
        {
            "lane": "reading",
            "source": "medium",
            "source_id": a["id"],
            "title": a["title"],
            "why_it_matters": a["why"] or a.get("subtitle", ""),
            "suggested_next_step": "Read it",
            "priority": 2,
            "deep_link": a.get("url", ""),
        }
        for a in ranked
    ]
