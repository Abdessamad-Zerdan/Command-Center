"""Calendar ingestion: today's events + tomorrow's first 3."""

import json
from datetime import datetime, timedelta

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from command_center.config import TZ
from command_center.sources import RawItem

TOMORROW_LIMIT = 3


def _parse_event_time(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ)


def _event_links(ev: dict) -> list[str]:
    links = []
    if ev.get("hangoutLink"):
        links.append(ev["hangoutLink"])
    for att in ev.get("attachments", []):
        if att.get("fileUrl"):
            links.append(att["fileUrl"])
    return links


def _event_to_raw_item(ev: dict) -> RawItem:
    attendees = [a.get("email") for a in ev.get("attendees", []) if a.get("email")]
    links = _event_links(ev)

    body_parts = []
    if attendees:
        body_parts.append("Attendees: " + ", ".join(attendees))
    if links:
        body_parts.append("Links: " + ", ".join(links))
    if ev.get("description"):
        body_parts.append(ev["description"][:400])

    start = ev.get("start", {}).get("dateTime") or ev.get("start", {}).get("date", "")
    end = ev.get("end", {}).get("dateTime") or ev.get("end", {}).get("date", "")

    return RawItem(
        source="calendar",
        source_id=ev["id"],
        title=ev.get("summary", "(no title)"),
        body="\n".join(body_parts),
        metadata={
            "deep_link": ev.get("htmlLink", ""),
            "attendees": attendees,
            "start_time": start,
            "end_time": end,
        },
    )


def _event_to_timeline_row(ev: dict) -> dict:
    attendees = [a.get("email") for a in ev.get("attendees", []) if a.get("email")]
    start = ev.get("start", {}).get("dateTime") or ev.get("start", {}).get("date", "")
    end = ev.get("end", {}).get("dateTime") or ev.get("end", {}).get("date", "")
    return {
        "title": ev.get("summary", "(no title)"),
        "start_time": start,
        "end_time": end,
        "attendees": json.dumps(attendees),
        "link": ev.get("hangoutLink"),
    }


class CalendarSource:
    def __init__(self, credentials: Credentials) -> None:
        self._service = build("calendar", "v3", credentials=credentials, cache_discovery=False)

    def fetch_with_events(self) -> tuple[list[RawItem], list[dict]]:
        now = datetime.now(TZ)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        tomorrow_start = today_start + timedelta(days=1)
        window_end = today_start + timedelta(days=2)

        resp = (
            self._service.events()
            .list(
                calendarId="primary",
                timeMin=today_start.isoformat(),
                timeMax=window_end.isoformat(),
                singleEvents=True,
                orderBy="startTime",
            )
            .execute()
        )
        events = resp.get("items", [])

        today_events = []
        tomorrow_events = []
        for ev in events:
            start_str = ev.get("start", {}).get("dateTime") or ev.get("start", {}).get("date")
            start_dt = _parse_event_time(start_str) if start_str else None
            if start_dt is None:
                continue
            if today_start <= start_dt < tomorrow_start:
                today_events.append(ev)
            elif tomorrow_start <= start_dt < window_end:
                tomorrow_events.append(ev)

        tomorrow_events = tomorrow_events[:TOMORROW_LIMIT]

        raw_items = [_event_to_raw_item(ev) for ev in today_events + tomorrow_events]
        timeline_rows = [_event_to_timeline_row(ev) for ev in today_events]
        return raw_items, timeline_rows

    def fetch(self) -> list[RawItem]:
        raw_items, _ = self.fetch_with_events()
        return raw_items

    def create_event(
        self, title: str, start: dict, end: dict, description: str = ""
    ) -> dict:
        """Creates a real event on the primary calendar. `start`/`end`
        are the Calendar API's own shape — {"dateTime": iso, "timeZone":
        tz_name} for a timed event, {"date": "YYYY-MM-DD"} for an
        all-day one — callers build whichever fits what they know about
        the item (see app.py's add-to-calendar route). Requires the
        calendar.events OAuth scope, not just calendar.readonly; raises
        googleapiclient.errors.HttpError (403) untouched if the saved
        token predates that scope — the caller maps that to an
        actionable re-auth message rather than a raw traceback.
        """
        body = {"summary": title, "start": start, "end": end}
        if description:
            body["description"] = description
        return self._service.events().insert(calendarId="primary", body=body).execute()
