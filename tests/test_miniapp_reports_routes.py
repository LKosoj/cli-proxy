from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from miniapp.route_context import MiniAppRouteContext
from miniapp.routes_reports import ReportsRouteServices, register_reports_routes


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_scope(session_uid: str) -> Any:
    scope = MagicMock()
    scope.session_uid = session_uid
    return scope


def _make_session(uid: str, workdir: str = "") -> Any:
    session = MagicMock()
    session.scope = None  # prevent MagicMock auto-attr from shadowing conversation_scope
    session.conversation_scope = _make_scope(uid)
    session.workdir = workdir
    session.id = uid.split(":")[-1] if ":" in uid else uid
    session.chat_id = int(uid.split(":")[0]) if ":" in uid else 1
    return session


def _make_bot_app(sessions: Dict[str, Any]) -> MagicMock:
    bot_app = MagicMock()
    manager = MagicMock()

    def sessions_for_chat(user_id: int) -> Dict[str, Any]:
        return {s.id: s for s in sessions.values()}

    manager.sessions_for_chat.side_effect = sessions_for_chat
    manager.sessions_by_chat = {1: {s.id: s for s in sessions.values()}}
    bot_app.manager = manager
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
    services = ReportsRouteServices(
        require_access=_fake_require_access(user_id=user_id, is_admin=is_admin),
        json_error=_json_error,
    )
    app = web.Application()
    register_reports_routes(app, ctx, services)
    return app


# ---------------------------------------------------------------------------
# Tests: GET /api/reports
# ---------------------------------------------------------------------------


def test_reports_list_no_session_uid() -> None:
    """Without session_uid, returns aggregated list across all visible sessions."""
    async def _run() -> None:
        session = _make_session("1:s1", workdir="/tmp/no_workdir_xyz")
        sessions = {"1:s1": session}
        bot_app = _make_bot_app(sessions)
        app = _make_app(bot_app)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/reports")
            assert resp.status == 200
            body = await resp.json()
            assert body["ok"] is True
            assert "reports" in body

    asyncio.run(_run())


def test_reports_list_with_md_files(tmp_path: Path) -> None:
    """Returns .md files from session workdir/.cli-proxy/.manager_reports."""
    workdir = str(tmp_path)
    reports_dir = tmp_path / ".cli-proxy" / ".manager_reports"
    reports_dir.mkdir(parents=True)
    (reports_dir / "report_20260101.md").write_text("# Report\nContent here.", encoding="utf-8")
    (reports_dir / "report_20260102.md").write_text("# Report 2\nMore content.", encoding="utf-8")

    async def _run() -> None:
        session = _make_session("1:s1", workdir=workdir)
        sessions = {"1:s1": session}
        bot_app = _make_bot_app(sessions)
        app = _make_app(bot_app)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/reports?session_uid=1%3As1")
            assert resp.status == 200
            body = await resp.json()
            assert body["ok"] is True
            names = {r["name"] for r in body["reports"]}
            assert "report_20260101.md" in names
            assert "report_20260102.md" in names

    asyncio.run(_run())


def test_reports_list_ignores_non_md_files(tmp_path: Path) -> None:
    """Non-.md files are not included in the listing."""
    workdir = str(tmp_path)
    reports_dir = tmp_path / ".cli-proxy" / ".manager_reports"
    reports_dir.mkdir(parents=True)
    (reports_dir / "data.csv").write_text("a,b,c", encoding="utf-8")
    (reports_dir / "report.md").write_text("# Markdown", encoding="utf-8")

    async def _run() -> None:
        session = _make_session("1:s1", workdir=workdir)
        bot_app = _make_bot_app({"1:s1": session})
        app = _make_app(bot_app)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/reports?session_uid=1%3As1")
            assert resp.status == 200
            body = await resp.json()
            names = [r["name"] for r in body["reports"]]
            assert "data.csv" not in names
            assert "report.md" in names

    asyncio.run(_run())


def test_reports_list_inaccessible_session() -> None:
    """session_uid not owned by user returns 403."""
    async def _run() -> None:
        session = _make_session("1:s1", workdir="/tmp")
        bot_app = _make_bot_app({"1:s1": session})
        app = _make_app(bot_app, user_id=1, is_admin=False)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/reports?session_uid=99%3As99")
            assert resp.status == 403

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Tests: GET /api/reports/{report_id}
# ---------------------------------------------------------------------------


def test_reports_content_returns_md_text(tmp_path: Path) -> None:
    """Returns markdown content of the requested report."""
    workdir = str(tmp_path)
    reports_dir = tmp_path / ".cli-proxy" / ".manager_reports"
    reports_dir.mkdir(parents=True)
    text = "# Hello\n\nThis is a report."
    (reports_dir / "report_a.md").write_text(text, encoding="utf-8")

    async def _run() -> None:
        session = _make_session("1:s1", workdir=workdir)
        bot_app = _make_bot_app({"1:s1": session})
        app = _make_app(bot_app)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/reports/report_a.md?session_uid=1%3As1")
            assert resp.status == 200
            body = await resp.json()
            assert body["ok"] is True
            assert body["content"] == text
            assert body["id"] == "report_a.md"

    asyncio.run(_run())


def test_reports_content_not_found(tmp_path: Path) -> None:
    """Returns 404 for missing report file."""
    workdir = str(tmp_path)
    reports_dir = tmp_path / ".cli-proxy" / ".manager_reports"
    reports_dir.mkdir(parents=True)

    async def _run() -> None:
        session = _make_session("1:s1", workdir=workdir)
        bot_app = _make_bot_app({"1:s1": session})
        app = _make_app(bot_app)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/reports/missing.md?session_uid=1%3As1")
            assert resp.status == 404

    asyncio.run(_run())


def test_reports_content_invalid_report_id() -> None:
    """Non-.md report_id is rejected as 400."""
    async def _run() -> None:
        session = _make_session("1:s1", workdir="/tmp")
        bot_app = _make_bot_app({"1:s1": session})
        app = _make_app(bot_app)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/reports/../../etc/passwd?session_uid=1%3As1")
            assert resp.status in (400, 404)

    asyncio.run(_run())


def test_reports_content_missing_session_uid() -> None:
    """Returns 400 when session_uid is omitted."""
    async def _run() -> None:
        bot_app = _make_bot_app({})
        app = _make_app(bot_app)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/reports/report.md")
            assert resp.status == 400

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Tests: GET /api/reports/{report_id}/download
# ---------------------------------------------------------------------------


def test_reports_download_md(tmp_path: Path) -> None:
    """Returns the raw MD file as attachment."""
    workdir = str(tmp_path)
    reports_dir = tmp_path / ".cli-proxy" / ".manager_reports"
    reports_dir.mkdir(parents=True)
    text = "# Download test"
    (reports_dir / "dl_report.md").write_text(text, encoding="utf-8")

    async def _run() -> None:
        session = _make_session("1:s1", workdir=workdir)
        bot_app = _make_bot_app({"1:s1": session})
        app = _make_app(bot_app)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get(
                "/api/reports/dl_report.md/download?session_uid=1%3As1&format=md"
            )
            assert resp.status == 200
            assert "attachment" in resp.headers.get("Content-Disposition", "")
            raw = await resp.read()
            assert raw == text.encode("utf-8")

    asyncio.run(_run())


def test_reports_download_pdf_not_available(tmp_path: Path) -> None:
    """PDF download returns 200 with an explanatory message (no conversion)."""
    workdir = str(tmp_path)
    reports_dir = tmp_path / ".cli-proxy" / ".manager_reports"
    reports_dir.mkdir(parents=True)
    (reports_dir / "report.md").write_text("# PDF test", encoding="utf-8")

    async def _run() -> None:
        session = _make_session("1:s1", workdir=workdir)
        bot_app = _make_bot_app({"1:s1": session})
        app = _make_app(bot_app)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get(
                "/api/reports/report.md/download?session_uid=1%3As1&format=pdf"
            )
            assert resp.status == 200
            text = await resp.text()
            assert "PDF" in text or "not available" in text.lower()

    asyncio.run(_run())


def test_reports_download_session_forbidden() -> None:
    """Download returns 403 for a session not owned by the user."""
    async def _run() -> None:
        session = _make_session("1:s1", workdir="/tmp")
        bot_app = _make_bot_app({"1:s1": session})
        app = _make_app(bot_app, user_id=1, is_admin=False)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get(
                "/api/reports/report.md/download?session_uid=99%3As99&format=md"
            )
            assert resp.status == 403

    asyncio.run(_run())
