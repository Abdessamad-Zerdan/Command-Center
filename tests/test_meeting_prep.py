"""Meeting prep holds only calendar events the user is attending — never a
promotional "add to calendar" mail, an unanswered invite or a task."""

from datetime import datetime, timedelta


from command_center.config import TZ
from command_center.sources import RawItem
from command_center.sources import calendar as calendar_source
from command_center.sources.gmail import _has_calendar_invite


def _raw(source: str, source_id: str, **metadata) -> RawItem:
    return RawItem(source=source, source_id=source_id, title="t", body="", metadata=metadata)


def _item(source: str, source_id: str, due_date=None) -> dict:
    return {"source": source, "source_id": source_id, "lane": "meeting_prep", "due_date": due_date}


def test_calendar_marks_the_users_response_and_drops_declined_events() -> None:
    from datetime import datetime, timedelta

    from command_center.config import TZ

    # Within today's window whenever the test runs.
    start = datetime.now(TZ).replace(hour=12, minute=0, second=0, microsecond=0)

    def event(eid, status=None):
        attendees = [{"email": "me@x.com", "self": True, "responseStatus": status}] if status else []
        return {"id": eid, "summary": eid, "start": {"dateTime": start.isoformat()},
                "end": {"dateTime": (start + timedelta(hours=1)).isoformat()}, "attendees": attendees}

    assert calendar_source._my_response(event("own")) == "accepted"
    assert calendar_source._my_response(event("inv", "needsAction")) == "needsAction"
    assert calendar_source._event_to_raw_item(event("t", "tentative")).metadata["my_response"] == "tentative"

    class _Call:
        def __init__(self, result):
            self.result = result

        def execute(self):
            return self.result

    class _Events:
        def list(self, **_):
            return _Call({"items": [event("keep", "accepted"), event("gone", "declined")]})

    class _Service:
        def events(self):
            return _Events()

    source = calendar_source.CalendarSource.__new__(calendar_source.CalendarSource)
    source._service = _Service()
    raw_items, timeline = source.fetch_with_events()
    assert [r.source_id for r in raw_items] == ["keep"]
    assert [row["title"] for row in timeline] == ["keep"]


def test_gmail_recognises_a_real_invite() -> None:
    invite = {"mimeType": "multipart/mixed", "parts": [
        {"mimeType": "text/plain", "body": {}},
        {"mimeType": "text/calendar; method=REQUEST", "body": {}},
    ]}
    attachment = {"mimeType": "multipart/mixed", "parts": [{"mimeType": "application/ics", "filename": "invite.ics"}]}
    promo = {"mimeType": "text/html", "body": {}}
    assert _has_calendar_invite(invite) and _has_calendar_invite(attachment)
    assert not _has_calendar_invite(promo)
