"""App settings, lane labels, day bounds, summary language, nudges, source configs, triage rules and corrections, setup invites.

Part of the queries package — see queries/__init__.py."""

from datetime import datetime
from typing import Any

from command_center.config import LANES, LANE_LABELS, TZ
from command_center.db import session


def list_source_configs() -> list[dict[str, Any]]:
    with session() as conn:
        rows = conn.execute(
            "SELECT * FROM source_config ORDER BY source_name ASC"
        ).fetchall()
        return [dict(row) for row in rows]



def seed_source_config(
    source_names: list[str],
    default_interval_minutes: int = 60,
    overrides: dict[str, int] | None = None,
) -> None:
    """Seeds a row per source, `last_pulled_at` set to now rather than
    NULL — that's what keeps the scheduler from firing an immediate pull
    on first boot, since it only fires once `interval_minutes` have
    elapsed since `last_pulled_at`. Existing rows are left untouched.

    `overrides` lets specific sources start at a different interval than
    `default_interval_minutes` (e.g. content-discovery sources that
    don't need same-day freshness) — only used the very first time a
    source's row is created, never retroactively.
    """
    now = datetime.now(TZ).isoformat()
    overrides = overrides or {}
    with session() as conn:
        for name in source_names:
            interval = overrides.get(name, default_interval_minutes)
            conn.execute(
                """
                INSERT OR IGNORE INTO source_config
                    (source_name, enabled, interval_minutes, last_pulled_at)
                VALUES (?, 1, ?, ?)
                """,
                (name, interval, now),
            )



def update_source_config(
    source_name: str, enabled: bool | None = None, interval_minutes: int | None = None
) -> None:
    with session() as conn:
        if enabled is not None:
            conn.execute(
                "UPDATE source_config SET enabled = ? WHERE source_name = ?",
                (1 if enabled else 0, source_name),
            )
        if interval_minutes is not None:
            conn.execute(
                "UPDATE source_config SET interval_minutes = ? WHERE source_name = ?",
                (interval_minutes, source_name),
            )



def touch_source_last_pulled(source_name: str) -> None:
    with session() as conn:
        conn.execute(
            "UPDATE source_config SET last_pulled_at = ? WHERE source_name = ?",
            (datetime.now(TZ).isoformat(), source_name),
        )



def list_triage_corrections(limit: int = 20) -> list[dict[str, Any]]:
    with session() as conn:
        rows = conn.execute(
            "SELECT * FROM triage_corrections ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]



def triage_correction_patterns(min_count: int = 2) -> list[dict[str, Any]]:
    """Groups corrections by (source, from_lane, to_lane) — a repeated
    pattern (moved the same way `min_count`+ times) is a real signal a
    triage rule would fix, not a one-off. Ordered by count desc so the
    strongest pattern surfaces first."""
    with session() as conn:
        rows = conn.execute(
            """
            SELECT source, from_lane, to_lane, COUNT(*) AS count,
                   MAX(title) AS example_title
            FROM triage_corrections
            GROUP BY source, from_lane, to_lane
            HAVING COUNT(*) >= ?
            ORDER BY count DESC
            """,
            (min_count,),
        ).fetchall()
        return [dict(row) for row in rows]



def list_triage_rules(enabled_only: bool = False) -> list[dict[str, Any]]:
    """In the order triage_rules.py applies them (first match wins): the
    user's own rules oldest first, then the built-in defaults — so this is
    also the order a user needs to see them in to know which one would win
    a conflict."""
    with session() as conn:
        sql = "SELECT * FROM triage_rules"
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY builtin ASC, id ASC"
        rows = conn.execute(sql).fetchall()
        return [dict(row) for row in rows]



def create_triage_rule(field: str, match_value: str, lane: str) -> int:
    with session() as conn:
        cursor = conn.execute(
            "INSERT INTO triage_rules (field, match_value, lane, enabled, created_at) "
            "VALUES (?, ?, ?, 1, ?)",
            (field, match_value, lane, datetime.now(TZ).isoformat()),
        )
        return cursor.lastrowid



def hide_builtin_noise_rules(pairs: tuple[tuple[str, str], ...]) -> None:
    """One-time switch of the built-in noise rules from Reading to Skip
    on an install seeded before that default changed. Only rows still
    built-in and still on 'reading' change — one the user edited keeps
    their choice. An app_settings flag makes it run once."""
    with session() as conn:
        if conn.execute("SELECT 1 FROM app_settings WHERE key = 'builtin_noise_rules_hidden'").fetchone():
            return
        for field, match_value in pairs:
            conn.execute(
                "UPDATE triage_rules SET lane = 'skip' "
                "WHERE builtin = 1 AND lane = 'reading' AND field = ? AND match_value = ?",
                (field, match_value),
            )
        conn.execute("INSERT INTO app_settings (key, value) VALUES ('builtin_noise_rules_hidden', '1')")


def seed_builtin_triage_rules(rules: tuple[tuple[str, str, str], ...]) -> None:
    """Inserts the built-in rules on an install's first start only — an
    app_settings flag records it, so a deleted default stays deleted."""
    with session() as conn:
        if conn.execute("SELECT 1 FROM app_settings WHERE key = 'builtin_triage_rules_seeded'").fetchone():
            return
        now = datetime.now(TZ).isoformat()
        for field, match_value, lane in rules:
            conn.execute(
                "INSERT INTO triage_rules (field, match_value, lane, enabled, builtin, created_at) "
                "VALUES (?, ?, ?, 1, 1, ?)",
                (field, match_value, lane, now),
            )
        conn.execute("INSERT INTO app_settings (key, value) VALUES ('builtin_triage_rules_seeded', '1')")



def set_triage_rule_enabled(rule_id: int, enabled: bool) -> bool:
    with session() as conn:
        cursor = conn.execute(
            "UPDATE triage_rules SET enabled = ? WHERE id = ?", (1 if enabled else 0, rule_id)
        )
        return cursor.rowcount > 0



def delete_triage_rule(rule_id: int) -> bool:
    with session() as conn:
        cursor = conn.execute("DELETE FROM triage_rules WHERE id = ?", (rule_id,))
        return cursor.rowcount > 0



_DEFAULT_APP_SETTINGS = {"day_bounds_start": "07:00", "day_bounds_end": "22:00"}



def seed_app_settings() -> None:
    """Seeds default day-bounds settings on first run. INSERT OR IGNORE,
    same idiom as seed_source_config — existing rows are left untouched."""
    with session() as conn:
        for key, value in _DEFAULT_APP_SETTINGS.items():
            conn.execute(
                "INSERT OR IGNORE INTO app_settings (key, value) VALUES (?, ?)",
                (key, value),
            )



def get_day_bounds() -> tuple[str, str]:
    """Returns (start "HH:MM", end "HH:MM"). Always returns a value —
    seed_app_settings() runs at every startup, same guarantee source_config
    rows have."""
    with session() as conn:
        rows = conn.execute(
            "SELECT key, value FROM app_settings WHERE key IN ('day_bounds_start', 'day_bounds_end')"
        ).fetchall()
    values = {row["key"]: row["value"] for row in rows}
    return (
        values.get("day_bounds_start", _DEFAULT_APP_SETTINGS["day_bounds_start"]),
        values.get("day_bounds_end", _DEFAULT_APP_SETTINGS["day_bounds_end"]),
    )



def get_app_value(key: str) -> str | None:
    with session() as conn:
        row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_app_value(key: str, value: str) -> None:
    with session() as conn:
        conn.execute(
            "INSERT INTO app_settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )



DEFAULT_SUMMARY_LANGUAGE = "English"



def get_summary_language() -> str:
    """The one language every triage summary and "Next:" line is written
    in (Settings → Summary language), whatever language the mail itself
    is in. English unless the user changed it."""
    with session() as conn:
        row = conn.execute("SELECT value FROM app_settings WHERE key = 'summary_language'").fetchone()
    return row["value"] if row and row["value"].strip() else DEFAULT_SUMMARY_LANGUAGE



def set_summary_language(language: str) -> None:
    with session() as conn:
        conn.execute(
            "INSERT INTO app_settings (key, value) VALUES ('summary_language', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (language,),
        )



def get_lane_labels() -> dict[str, str]:
    """config.LANE_LABELS (from profile.py) as the default, overlaid with
    any per-lane renames saved via /settings/lane-labels. Renaming a
    lane's *display text* never touches the lane slugs themselves
    (config.LANES) — triage, the DB `lane` column, and chat tool schemas
    all keep using urgent/action_items/etc. regardless."""
    labels = dict(LANE_LABELS)
    with session() as conn:
        rows = conn.execute(
            "SELECT key, value FROM app_settings WHERE key LIKE 'lane_label_%'"
        ).fetchall()
    for row in rows:
        lane = row["key"].removeprefix("lane_label_")
        if lane in labels:
            labels[lane] = row["value"]
    return labels



def set_lane_label(lane: str, label: str) -> bool:
    """Blank `label` clears the override, reverting to config.LANE_LABELS'
    default rather than saving an empty string. Returns False for an
    unknown lane."""
    if lane not in LANES:
        return False
    key = f"lane_label_{lane}"
    with session() as conn:
        if not label.strip():
            conn.execute("DELETE FROM app_settings WHERE key = ?", (key,))
        else:
            conn.execute(
                "INSERT INTO app_settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, label.strip()),
            )
    return True



def set_day_bounds(start: str, end: str) -> None:
    with session() as conn:
        conn.execute(
            "INSERT INTO app_settings (key, value) VALUES ('day_bounds_start', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (start,),
        )
        conn.execute(
            "INSERT INTO app_settings (key, value) VALUES ('day_bounds_end', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (end,),
        )



def was_nudge_notified_today(nudge_id: str, today: str) -> bool:
    """One ntfy push per nudge id per day — the coordinator tick runs
    every 5 minutes, so without this a still-unresolved nudge would
    spam a push every tick instead of once when it first appears."""
    with session() as conn:
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key = ?", (f"ntfy_last_sent_{nudge_id}",)
        ).fetchone()
    return row is not None and row["value"] == today



def mark_nudge_notified(nudge_id: str, today: str) -> None:
    with session() as conn:
        conn.execute(
            "INSERT INTO app_settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (f"ntfy_last_sent_{nudge_id}", today),
        )



def create_setup_invite(email: str, token: str, expires_at: str) -> int:
    now = datetime.now(TZ).isoformat()
    with session() as conn:
        cursor = conn.execute(
            "INSERT INTO setup_invites (email, token, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (email, token, now, expires_at),
        )
        return cursor.lastrowid



def get_setup_invite_by_token(token: str) -> dict[str, Any] | None:
    with session() as conn:
        row = conn.execute("SELECT * FROM setup_invites WHERE token = ?", (token,)).fetchone()
        return dict(row) if row is not None else None



def list_setup_invites() -> list[dict[str, Any]]:
    with session() as conn:
        # id DESC as a tiebreaker — two invites created back-to-back can
        # get the exact same created_at timestamp string (seen in
        # practice, not just theoretical), and created_at DESC alone
        # leaves SQLite's tie-break order undefined, which showed up as
        # a real intermittent test failure. id always reflects true
        # insertion order regardless.
        rows = conn.execute(
            "SELECT * FROM setup_invites ORDER BY created_at DESC, id DESC"
        ).fetchall()
        return [dict(row) for row in rows]



def mark_setup_invite_used(token: str, used_by_ip: str | None) -> None:
    now = datetime.now(TZ).isoformat()
    with session() as conn:
        conn.execute(
            "UPDATE setup_invites SET used_at = ?, used_by_ip = ? WHERE token = ?",
            (now, used_by_ip, token),
        )
