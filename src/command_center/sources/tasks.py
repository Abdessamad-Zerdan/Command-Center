"""Google Tasks ingestion: incomplete tasks across all task lists.

Also holds create/update/complete helpers, used by the assistant's
tool-calling dispatch (assistant/tools.py) — not by the brief pipeline.
"""

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from command_center.sources import RawItem


class TasksSource:
    def __init__(self, credentials: Credentials) -> None:
        self._service = build("tasks", "v1", credentials=credentials, cache_discovery=False)

    def fetch(self) -> list[RawItem]:
        tasklists = self._service.tasklists().list().execute().get("items", [])

        items = []
        for tasklist in tasklists:
            tasklist_id = tasklist["id"]
            resp = (
                self._service.tasks()
                .list(tasklist=tasklist_id, showCompleted=False)
                .execute()
            )
            for task in resp.get("items", []):
                items.append(
                    RawItem(
                        source="google_tasks",
                        source_id=task["id"],
                        title=task.get("title") or "(untitled task)",
                        body=task.get("notes", ""),
                        metadata={
                            "deep_link": "",
                            "tasklist_id": tasklist_id,
                            "due": task.get("due", ""),
                        },
                    )
                )
        return items


def create_task(
    credentials: Credentials, tasklist_id: str, title: str, notes: str = "", due: str = ""
) -> dict:
    service = build("tasks", "v1", credentials=credentials, cache_discovery=False)
    body: dict = {"title": title}
    if notes:
        body["notes"] = notes
    if due:
        body["due"] = due
    return service.tasks().insert(tasklist=tasklist_id, body=body).execute()


def update_task(
    credentials: Credentials,
    tasklist_id: str,
    task_id: str,
    title: str | None = None,
    notes: str | None = None,
    due: str | None = None,
) -> dict:
    service = build("tasks", "v1", credentials=credentials, cache_discovery=False)
    body: dict = {}
    if title is not None:
        body["title"] = title
    if notes is not None:
        body["notes"] = notes
    if due is not None:
        body["due"] = due
    return service.tasks().patch(tasklist=tasklist_id, task=task_id, body=body).execute()


def complete_task(credentials: Credentials, tasklist_id: str, task_id: str) -> dict:
    service = build("tasks", "v1", credentials=credentials, cache_discovery=False)
    return (
        service.tasks()
        .patch(tasklist=tasklist_id, task=task_id, body={"status": "completed"})
        .execute()
    )
