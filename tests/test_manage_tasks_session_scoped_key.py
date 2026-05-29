import pytest

from agent.plugins.manage_tasks import ManageTasksTool


@pytest.mark.asyncio
async def test_manage_tasks_segregates_same_raw_session_id_by_scoped_key() -> None:
    plugin = ManageTasksTool()
    plugin.initialize(config=None, services={})

    first_ctx = {"session_id": "s1", "session_scoped_key": "1_s1"}
    second_ctx = {"session_id": "s1", "session_scoped_key": "2_s1"}

    added_first = await plugin.execute(
        {"action": "add", "tasks": [{"id": "t1", "content": "chat one task"}]},
        first_ctx,
    )
    added_second = await plugin.execute(
        {"action": "add", "tasks": [{"id": "t2", "content": "chat two task"}]},
        second_ctx,
    )
    listed_first = await plugin.execute({"action": "list"}, first_ctx)
    listed_second = await plugin.execute({"action": "list"}, second_ctx)

    assert added_first["success"] is True
    assert added_second["success"] is True
    assert "chat one task" in listed_first["output"]
    assert "chat two task" not in listed_first["output"]
    assert "chat two task" in listed_second["output"]
    assert "chat one task" not in listed_second["output"]


@pytest.mark.asyncio
async def test_manage_tasks_segregates_runs_within_same_scoped_session() -> None:
    plugin = ManageTasksTool()
    plugin.initialize(config=None, services={})

    first_ctx = {"session_id": "s1", "session_scoped_key": "1_s1", "run_id": "run-a"}
    second_ctx = {"session_id": "s1", "session_scoped_key": "1_s1", "run_id": "run-b"}

    added_first = await plugin.execute(
        {"action": "add", "tasks": [{"id": "t1", "content": "first run task"}]},
        first_ctx,
    )
    added_second = await plugin.execute(
        {"action": "add", "tasks": [{"id": "t2", "content": "second run task"}]},
        second_ctx,
    )
    listed_first = await plugin.execute({"action": "list"}, first_ctx)
    listed_second = await plugin.execute({"action": "list"}, second_ctx)

    assert added_first["success"] is True
    assert added_second["success"] is True
    assert "first run task" in listed_first["output"]
    assert "second run task" not in listed_first["output"]
    assert "second run task" in listed_second["output"]
    assert "first run task" not in listed_second["output"]


@pytest.mark.asyncio
async def test_manage_tasks_returns_structured_progress_payload() -> None:
    plugin = ManageTasksTool()
    plugin.initialize(config=None, services={})

    added = await plugin.execute(
        {
            "action": "add",
            "tasks": [
                {"id": "t1", "content": "Done", "status": "completed"},
                {"id": "t2", "content": "Next", "status": "pending"},
            ],
        },
        {"session_id": "s1", "session_scoped_key": "1_s1"},
    )

    assert added["success"] is True
    assert added["manage_tasks"]["changed"] is True
    assert added["manage_tasks"]["progress"] == {"total": 2, "closed": 1, "open": 1}
