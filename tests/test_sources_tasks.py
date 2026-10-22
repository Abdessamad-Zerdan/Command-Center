from command_center.sources import tasks as tasks_module


class _FakeExecutable:
    def __init__(self, result: dict) -> None:
        self._result = result

    def execute(self) -> dict:
        return self._result


class _FakeTasksResource:
    def __init__(self, tasks_by_list: dict[str, list[dict]]) -> None:
        self._tasks_by_list = tasks_by_list
        self.inserted: list[dict] = []
        self.patched: list[dict] = []

    def list(self, tasklist: str, showCompleted: bool | None = None) -> _FakeExecutable:
        return _FakeExecutable({"items": self._tasks_by_list.get(tasklist, [])})

    def insert(self, tasklist: str, body: dict) -> _FakeExecutable:
        self.inserted.append({"tasklist": tasklist, "body": body})
        return _FakeExecutable({"id": "new-task-id", **body})

    def patch(self, tasklist: str, task: str, body: dict) -> _FakeExecutable:
        self.patched.append({"tasklist": tasklist, "task": task, "body": body})
        return _FakeExecutable({"id": task, **body})


class _FakeTasklistsResource:
    def __init__(self, tasklists: list[dict]) -> None:
        self._tasklists = tasklists

    def list(self) -> _FakeExecutable:
        return _FakeExecutable({"items": self._tasklists})


class _FakeTasksService:
    def __init__(self, tasklists: list[dict], tasks_by_list: dict[str, list[dict]]) -> None:
        self.tasklists_resource = _FakeTasklistsResource(tasklists)
        self.tasks_resource = _FakeTasksResource(tasks_by_list)

    def tasklists(self) -> _FakeTasklistsResource:
        return self.tasklists_resource

    def tasks(self) -> _FakeTasksResource:
        return self.tasks_resource


def _fake_build(service):
    def _build(service_name, version, credentials=None, cache_discovery=False):
        return service

    return _build


def test_fetch_maps_incomplete_tasks_across_all_lists(monkeypatch) -> None:
    service = _FakeTasksService(
        tasklists=[{"id": "list1"}, {"id": "list2"}],
        tasks_by_list={
            "list1": [
                {"id": "t1", "title": "Buy milk", "notes": "2%", "due": "2026-08-15T00:00:00Z"}
            ],
            "list2": [{"id": "t2", "title": "File taxes"}],
        },
    )
    monkeypatch.setattr(tasks_module, "build", _fake_build(service))

    items = tasks_module.TasksSource(credentials=object()).fetch()

    assert len(items) == 2
    by_id = {i.source_id: i for i in items}
    assert by_id["t1"].source == "google_tasks"
    assert by_id["t1"].title == "Buy milk"
    assert by_id["t1"].body == "2%"
    assert by_id["t1"].metadata["tasklist_id"] == "list1"
    # No stable per-task web URL in the Tasks API — deep_link stays empty,
    # which item_card.html already renders without an Open link.
    assert by_id["t1"].metadata["deep_link"] == ""
    assert by_id["t2"].title == "File taxes"
    assert by_id["t2"].body == ""
    assert by_id["t2"].metadata["tasklist_id"] == "list2"


def test_fetch_defaults_untitled_task(monkeypatch) -> None:
    service = _FakeTasksService(
        tasklists=[{"id": "list1"}],
        tasks_by_list={"list1": [{"id": "t1"}]},
    )
    monkeypatch.setattr(tasks_module, "build", _fake_build(service))

    items = tasks_module.TasksSource(credentials=object()).fetch()

    assert items[0].title == "(untitled task)"


def test_fetch_returns_empty_list_when_no_tasklists(monkeypatch) -> None:
    service = _FakeTasksService(tasklists=[], tasks_by_list={})
    monkeypatch.setattr(tasks_module, "build", _fake_build(service))

    assert tasks_module.TasksSource(credentials=object()).fetch() == []


def test_create_task_sends_expected_body(monkeypatch) -> None:
    service = _FakeTasksService(tasklists=[], tasks_by_list={})
    monkeypatch.setattr(tasks_module, "build", _fake_build(service))

    result = tasks_module.create_task(
        credentials=object(),
        tasklist_id="list1",
        title="New task",
        notes="notes",
        due="2026-08-20T00:00:00Z",
    )

    assert result["id"] == "new-task-id"
    assert service.tasks_resource.inserted == [
        {
            "tasklist": "list1",
            "body": {
                "title": "New task",
                "notes": "notes",
                "due": "2026-08-20T00:00:00Z",
            },
        }
    ]


def test_create_task_omits_unset_optional_fields(monkeypatch) -> None:
    service = _FakeTasksService(tasklists=[], tasks_by_list={})
    monkeypatch.setattr(tasks_module, "build", _fake_build(service))

    tasks_module.create_task(credentials=object(), tasklist_id="list1", title="Bare task")

    assert service.tasks_resource.inserted == [
        {"tasklist": "list1", "body": {"title": "Bare task"}}
    ]


def test_update_task_only_includes_provided_fields(monkeypatch) -> None:
    service = _FakeTasksService(tasklists=[], tasks_by_list={})
    monkeypatch.setattr(tasks_module, "build", _fake_build(service))

    tasks_module.update_task(
        credentials=object(), tasklist_id="list1", task_id="t1", title="Renamed"
    )

    assert service.tasks_resource.patched == [
        {"tasklist": "list1", "task": "t1", "body": {"title": "Renamed"}}
    ]


def test_complete_task_sets_status_completed(monkeypatch) -> None:
    service = _FakeTasksService(tasklists=[], tasks_by_list={})
    monkeypatch.setattr(tasks_module, "build", _fake_build(service))

    tasks_module.complete_task(credentials=object(), tasklist_id="list1", task_id="t1")

    assert service.tasks_resource.patched == [
        {"tasklist": "list1", "task": "t1", "body": {"status": "completed"}}
    ]
