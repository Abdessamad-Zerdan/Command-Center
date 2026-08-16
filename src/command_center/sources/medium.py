"""Medium personal feed: unofficial RapidAPI (medium2.p.rapidapi.com).

Explicitly less reliable than the Google-backed sources — every network
call here is independently try/excepted, and pipeline.py wraps the whole
fetch_and_rank() call too, so a failure here only degrades the Reading
lane, never the rest of the brief.
"""

import json
import logging

import httpx
import openai

from command_center.config import (
    GROQ_API_KEY,
    MEDIUM_API_KEY,
    MEDIUM_RANKING_CONTEXT,
    MEDIUM_USER_ID_CACHE,
    MEDIUM_USERNAME,
    TRIAGE_MODEL,
)
from command_center.triage import TriageProviderError

logger = logging.getLogger(__name__)

BASE_URL = "https://medium2.p.rapidapi.com"
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
    except Exception:
        logger.exception("Medium interests fetch failed, skipping")
        return []


def _fetch_following(client: httpx.Client, user_id: str) -> list[str]:
    try:
        resp = client.get(f"{BASE_URL}/user/{user_id}/following", headers=_headers())
        resp.raise_for_status()
        data = resp.json()
        return data.get("following", []) if isinstance(data, dict) else data
    except Exception:
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
    except Exception:
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
        except Exception:
            logger.exception("Medium article detail fetch failed for %s, skipping it", article_id)
    return articles


def _rank_articles(articles: list[dict], interests: list[str]) -> list[dict]:
    if not GROQ_API_KEY:
        raise TriageProviderError("Medium ranking needs GROQ_API_KEY but it is not set in .env.")
    if not articles:
        return []

    client = openai.OpenAI(base_url="https://api.groq.com/openai/v1", api_key=GROQ_API_KEY)
    payload = {
        "interests": interests,
        "articles": [
            {"id": a["id"], "title": a["title"], "subtitle": a["subtitle"]} for a in articles
        ],
    }

    try:
        response = client.chat.completions.create(
            model=TRIAGE_MODEL,
            max_tokens=1024,
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "system",
                    "content": (
                        f"Pick the 3 most relevant articles to this person's current work in "
                        f"{MEDIUM_RANKING_CONTEXT} (their stated interests are extra context). "
                        'Respond with ONLY a JSON object: {"picks": [{"id": string, "why": '
                        'string}]}, at most 3 picks, most relevant first, "why" as one short '
                        "sentence. No other text."
                    ),
                },
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
        )
    except openai.AuthenticationError as exc:
        raise TriageProviderError(
            f"Medium ranking failed: Groq authentication error ({exc})."
        ) from exc
    except (openai.APIConnectionError, openai.NotFoundError) as exc:
        raise TriageProviderError(f"Medium ranking failed: Groq unreachable ({exc}).") from exc

    content = response.choices[0].message.content
    if not content:
        raise TriageProviderError("Medium ranking: Groq returned an empty response.")

    try:
        picks = json.loads(content).get("picks", [])
    except json.JSONDecodeError as exc:
        raise TriageProviderError("Medium ranking: Groq returned malformed JSON.") from exc

    by_id = {a["id"]: a for a in articles}
    ranked = []
    for pick in picks:
        article = by_id.get(pick.get("id"))
        if article is None:
            continue
        ranked.append({**article, "why": pick.get("why", "")})
    return ranked[:RANK_COUNT]


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
