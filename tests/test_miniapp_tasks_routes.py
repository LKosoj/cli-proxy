from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from typing import Any, Dict, List
from unittest.mock import MagicMock
from urllib.parse import quote

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from miniapp.route_context import MiniAppRouteContext
from miniapp.routes_tasks import TasksRouteServices, register_tasks_routes


def _build_init_data(bot_token: str, user_id: int) -> str:
    payload = {
        "auth_date": str(int(time.time())),
        "query_id": "q1",
        "user": json.dumps(
            {"id": user_id, "username": f"user{user_id}", "first_name": f"User{user_id}"},
            ensure_ascii=False,
        ),
    }
    check = "\n".join(f"{key}={value}" for key, value in sorted(payload.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    sig = hmac.new(secret, check.encode("utf-8"), hashlib.sha256).hexdigest()
    return (
        f"auth_date={payload['auth_date']}"
        f"&query_id=q1"
        f"&user={quote(payload['user'])}"
        f"&hash={sig}"
    )


def _make_done_task() -> asyncio.Task:
    loop = asyncio.new_event_loop()
    try:
        t = loop.create_task(asyncio.sleep(0))
        loop.run_until_complete(t)
    finally:
        loop.close()

    class _FakeDoneTask:
        def done(self) -> bool:
            return True

    return _FakeDoneTask()  # type: ignore[return-value]


class _FakeTaskRecord:
    def __init__(self, name: str, *, done: bool = False) -> None:
        self.name = name
        self._done = done

        class _FakeTask:
            def __init__(self, done_val: bool) -> None:
                self._done = done_val

            def done(self) -> bool:
                return self._done

        self.task = _FakeTask(done)


class _FakeModeTasks:
    def __init__(self, tasks: Dict[Any, List[_FakeTaskRecord]] | None = None) -> None:
        self.tasks: Dict[Any, List[_FakeTaskRecord]] = tasks or {}
        self._cancel_calls: List[str] = []

    async def cancel_session(
        self,
        *,
        session_uid: str | None = None,
        session_id: str | None = None,
        timeout_s: float = 1.0,
    ) -> int:
        uid = str(session_uid or session_id or "")
        self._cancel_calls.append(uid)
        count = 0
        for (suid, _mode_id), records in list(self.tasks.items()):
            if suid == uid:
                count += len([r for r in records if not r.task.done()])
        return count


class _FakeScope:
    def __init__(self, session_uid: str) -> None:
        self.session_uid = session_uid


class _FakeSession:
    def __init__(self, uid: str, session_id: str = "s1", chat_id: int = 1) -> None:
        self.conversation_scope = _FakeScope(uid)
        self.id = session_id
        self.chat_id = chat_id


def _make_bot_app(
    *,
    user_id: int = 1,
    session_uid: str = "1:s1",
    mode_tasks: _FakeModeTasks | None = None,
) -> MagicMock:
    bot_app = MagicMock()
    session = _FakeSession(session_uid)

    manager = MagicMock()
    manager.sessions_for_chat.return_value = {"s1": session}
    manager.sessions_by_chat = {user_id: {"s1": session}}
    bot_app.manager = manager

    bot_app.mode_tasks = mode_tasks or _FakeModeTasks()
    return bot_app


def _fake_require_access(user_id: int = 1, is_admin: bool = True) -> Any:
    async def _require(request: web.Request) -> Dict[str, Any]:
        return {"user_id": user_id, "is_admin": is_admin, "actor_id": f"tg:{user_id}"}

    return _require


async def _json_error(status: int, message: Any) -> web.Response:
    return web.json_response({"ok": False, "error": str(message or "")}, status=status)


def _make_app(bot_app: MagicMock, *, user_id: int = 1, is_admin: bool = True) -> web.Application:
    import logging

    ctx = MiniAppRouteContext(bot_app=bot_app, logger=logging.getLogger("test"))
    services = TasksRouteServices(
        require_access=_fake_require_access(user_id=user_id, is_admin=is_admin),
        json_error=_json_error,
    )
    app = web.Application()
    register_tasks_routes(app, ctx, services)
    return app


# ---- tests ----

def test_tasks_list_empty() -> None:
    """GET /api/tasks returns empty list when no tasks running."""
    async def _run() -> None:
        bot_app = _make_bot_app(mode_tasks=_FakeModeTasks())
        app = _make_app(bot_app)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/tasks")
            assert resp.status == 200
            body = await resp.json()
            assert body["ok"] is True
            assert body["tasks"] == []

    asyncio.run(_run())


def test_tasks_list_with_active_task() -> None:
    """GET /api/tasks returns running tasks for visible sessions."""
    async def _run() -> None:
        session_uid = "1:s1"
        records = [_FakeTaskRecord("my-task", done=False)]
        mode_tasks = _FakeModeTasks(tasks={(session_uid, "agent"): records})
        bot_app = _make_bot_app(session_uid=session_uid, mode_tasks=mode_tasks)
        app = _make_app(bot_app)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/tasks")
            assert resp.status == 200
            body = await resp.json()
            assert body["ok"] is True
            assert len(body["tasks"]) == 1
            task = body["tasks"][0]
            assert task["session_uid"] == session_uid
            assert task["mode_id"] == "agent"
            assert task["name"] == "my-task"
            assert task["status"] == "running"

    asyncio.run(_run())


def test_tasks_list_filters_done_tasks() -> None:
    """GET /api/tasks skips tasks that are already done."""
    async def _run() -> None:
        session_uid = "1:s1"
        records = [
            _FakeTaskRecord("done-task", done=True),
            _FakeTaskRecord("running-task", done=False),
        ]
        mode_tasks = _FakeModeTasks(tasks={(session_uid, "manager"): records})
        bot_app = _make_bot_app(session_uid=session_uid, mode_tasks=mode_tasks)
        app = _make_app(bot_app)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/tasks")
            assert resp.status == 200
            body = await resp.json()
            tasks = body["tasks"]
            assert len(tasks) == 1
            assert tasks[0]["name"] == "running-task"

    asyncio.run(_run())


def test_tasks_list_filters_inaccessible_session() -> None:
    """GET /api/tasks does not expose tasks from other users' sessions."""
    async def _run() -> None:
        other_uid = "99:s99"
        my_uid = "1:s1"
        records = [_FakeTaskRecord("secret-task", done=False)]
        mode_tasks = _FakeModeTasks(tasks={(other_uid, "agent"): records})
        bot_app = _make_bot_app(session_uid=my_uid, mode_tasks=mode_tasks)
        app = _make_app(bot_app)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/tasks")
            assert resp.status == 200
            body = await resp.json()
            # other session's task should not appear (not in visible sessions)
            assert body["tasks"] == []

    asyncio.run(_run())


def test_tasks_cancel_calls_mode_tasks() -> None:
    """POST /api/tasks/{session_uid}/cancel calls mode_tasks.cancel_session."""
    async def _run() -> None:
        session_uid = "1:s1"
        records = [_FakeTaskRecord("my-task", done=False)]
        mode_tasks = _FakeModeTasks(tasks={(session_uid, "agent"): records})
        bot_app = _make_bot_app(session_uid=session_uid, mode_tasks=mode_tasks)
        app = _make_app(bot_app)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(f"/api/tasks/{quote(session_uid, safe='')}/cancel")
            assert resp.status == 200
            body = await resp.json()
            assert body["ok"] is True
            assert body["session_uid"] == session_uid
            assert session_uid in mode_tasks._cancel_calls

    asyncio.run(_run())


def test_tasks_cancel_forbidden_for_other_session() -> None:
    """POST /api/tasks/{session_uid}/cancel returns 403 for inaccessible session."""
    async def _run() -> None:
        my_uid = "1:s1"
        other_uid = "99:s99"
        bot_app = _make_bot_app(session_uid=my_uid)
        app = _make_app(bot_app)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(f"/api/tasks/{quote(other_uid, safe='')}/cancel")
            assert resp.status == 403

    asyncio.run(_run())


def test_tasks_list_session_uid_filter() -> None:
    """GET /api/tasks?session_uid=... filters to requested session."""
    async def _run() -> None:
        session_uid = "1:s1"
        records = [_FakeTaskRecord("task-a", done=False)]
        mode_tasks = _FakeModeTasks(tasks={(session_uid, "agent"): records})
        bot_app = _make_bot_app(session_uid=session_uid, mode_tasks=mode_tasks)
        app = _make_app(bot_app)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get(f"/api/tasks?session_uid={quote(session_uid)}")
            assert resp.status == 200
            body = await resp.json()
            assert len(body["tasks"]) == 1

            # Non-accessible uid
            resp2 = await client.get("/api/tasks?session_uid=99:s99")
            assert resp2.status == 403

    asyncio.run(_run())
