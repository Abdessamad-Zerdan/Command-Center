"""Hardcoded fixture data so the dashboard can be built/reviewed before OAuth exists.

seed() is idempotent: items are deduped on (source, source_id), same as real
triage will be, so re-running the app doesn't duplicate fixture rows.
"""

import json
from datetime import datetime, timedelta

from command_center.config import TZ
from command_center.db import session


def _today() -> str:
    return datetime.now(TZ).date().isoformat()


def _fixture_items(brief_date: str) -> list[dict]:
    return [
        # urgent
        {
            "lane": "urgent",
            "source": "gmail",
            "source_id": "18c9f2a1b3d4e5f6",
            "title": "Production alert: payment webhook failing (3rd retry)",
            "why_it_matters": "Webhook consumer has been returning 500s for 40 minutes; "
            "downstream orders are queuing instead of confirming.",
            "suggested_next_step": "Check the consumer logs and redeploy or roll back.",
            "priority": 1,
            "deep_link": "https://mail.google.com/mail/u/0/#inbox/18c9f2a1b3d4e5f6",
        },
        {
            "lane": "urgent",
            "source": "gmail",
            "source_id": "18c9f2a1b3d4e600",
            "title": "Contrat à signer avant vendredi — Client XYZ",
            "why_it_matters": "«Le client attend la version signée avant vendredi 17h, "
            "sinon on perd le créneau d'onboarding.»",
            "suggested_next_step": "Relire la clause 4.2 et renvoyer signé aujourd'hui.",
            "priority": 1,
            "deep_link": "https://mail.google.com/mail/u/0/#inbox/18c9f2a1b3d4e600",
        },
        # action_items
        {
            "lane": "action_items",
            "source": "gmail",
            "source_id": "18c9f2a1b3d4e601",
            "title": "Can you review PR #482 before EOD?",
            "why_it_matters": "Blocks tomorrow's deploy — teammate is waiting on your sign-off.",
            "suggested_next_step": "Review the diff, focus on the migration in models.py.",
            "priority": 2,
            "deep_link": "https://mail.google.com/mail/u/0/#inbox/18c9f2a1b3d4e601",
        },
        {
            "lane": "action_items",
            "source": "notion",
            "source_id": "notion-page-q3-retro",
            "title": "Write Q3 retro doc",
            "why_it_matters": "Retro meeting is Thursday; doc is still empty.",
            "suggested_next_step": "Draft the three sections: wins, misses, action items.",
            "priority": 2,
            "deep_link": "https://notion.so/notion-page-q3-retro",
        },
        # meeting_prep
        {
            "lane": "meeting_prep",
            "source": "calendar",
            "source_id": "cal-event-acme-call",
            "title": "Client call — Acme Corp @ 11:00",
            "why_it_matters": "Decision meeting on the pricing tier change; 4 attendees, "
            "no agenda doc shared yet.",
            "suggested_next_step": "Skim last week's thread with Acme and jot 3 talking points.",
            "priority": 1,
            "deep_link": "https://calendar.google.com/calendar/u/0/r/eventedit/cal-event-acme-call",
        },
        {
            "lane": "meeting_prep",
            "source": "calendar",
            "source_id": "cal-event-1-1-manager",
            "title": "1:1 with manager @ 15:00",
            "why_it_matters": "Recurring sync — no prep needed beyond your running notes doc.",
            "suggested_next_step": "Add this week's blockers to the shared doc.",
            "priority": 3,
            "deep_link": "https://calendar.google.com/calendar/u/0/r/eventedit/cal-event-1-1-manager",
        },
        # tasks_due
        {
            "lane": "tasks_due",
            "source": "notion",
            "source_id": "notion-page-ssl-renew",
            "title": "Renew SSL cert for staging",
            "why_it_matters": "Due today — cert expires in 48h if not rotated.",
            "suggested_next_step": "Run the renewal script and confirm the new expiry date.",
            "priority": 2,
            "deep_link": "https://notion.so/notion-page-ssl-renew",
        },
        {
            "lane": "tasks_due",
            "source": "notion",
            "source_id": "notion-page-rapport-masarif",
            "title": "Sali rapport dyal les frais qbel Friday",
            "why_it_matters": "Comptabilité khassha les recus dyal ce mois bach ts9od closing.",
            "suggested_next_step": "Jam3 les recus w rsel rapport l comptable.",
            "priority": 3,
            "deep_link": "https://notion.so/notion-page-rapport-masarif",
        },
    ]


def _fixture_calendar_events(brief_date: str) -> list[dict]:
    base = datetime.fromisoformat(brief_date).replace(hour=0, minute=0, tzinfo=TZ)
    slots = [
        (9, 30, 10, 0, "Standup", [], None),
        (
            11,
            0,
            12,
            0,
            "Client call — Acme Corp",
            ["you@company.com", "lead@acme.com", "pm@acme.com"],
            "https://meet.google.com/abc-defg-hij",
        ),
        (13, 0, 14, 0, "Lunch", [], None),
        (
            15,
            0,
            15,
            30,
            "1:1 with manager",
            ["you@company.com", "manager@company.com"],
            None,
        ),
        (17, 0, 17, 30, "Sprint planning prep", [], None),
    ]
    events = []
    for sh, sm, eh, em, title, attendees, link in slots:
        events.append(
            {
                "title": title,
                "start_time": (base + timedelta(hours=sh, minutes=sm)).isoformat(),
                "end_time": (base + timedelta(hours=eh, minutes=em)).isoformat(),
                "attendees": json.dumps(attendees),
                "link": link,
            }
        )
    return events


def seed() -> None:
    brief_date = _today()
    now = datetime.now(TZ).isoformat()

    with session() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO briefs (brief_date, generated_at, degraded_lanes, is_fixture) "
            "VALUES (?, ?, '[]', 1)",
            (brief_date, now),
        )

        for item in _fixture_items(brief_date):
            conn.execute(
                """
                INSERT OR IGNORE INTO items
                    (brief_date, lane, source, source_id, title, why_it_matters,
                     suggested_next_step, priority, deep_link, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    brief_date,
                    item["lane"],
                    item["source"],
                    item["source_id"],
                    item["title"],
                    item["why_it_matters"],
                    item["suggested_next_step"],
                    item["priority"],
                    item["deep_link"],
                    now,
                ),
            )

        existing = conn.execute(
            "SELECT COUNT(*) AS n FROM calendar_events WHERE brief_date = ?", (brief_date,)
        ).fetchone()["n"]
        if existing == 0:
            for ev in _fixture_calendar_events(brief_date):
                conn.execute(
                    """
                    INSERT INTO calendar_events
                        (brief_date, title, start_time, end_time, attendees, link)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        brief_date,
                        ev["title"],
                        ev["start_time"],
                        ev["end_time"],
                        ev["attendees"],
                        ev["link"],
                    ),
                )
