import asyncio
import hashlib
import hmac
import json
import logging
import time
from types import SimpleNamespace
from urllib.parse import quote

import yaml
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from app.services.ssh_config_loader import save_ssh_config
from app.services.logging_service import build_session_log_context
from app.services.session_tick_history_store import append_session_tick
from app.services.runtime_progress_service import emit_runtime_progress
from bot import BotApp
from config import AppConfig, DefaultsConfig, MCPConfig, MiniAppConfig, SSHHostConfig, TelegramConfig, ToolConfig
from miniapp.routes import MiniAppRoutes
from miniapp.services.config_service import app_config_to_dict
from miniapp.services.logs_service import LogAccessDeniedError, LogsService, ParsedLogEntry
from modes.agent.mode import agent_project_scope_key
from modes.analyst.state_store import AnalystStateStore, build_context_key
from modes.sdk.planning import save_plan
from modes.sdk.runtime.contracts import DevTask, ProjectPlan
from modes.webmaster.state_store import WebmasterStateStore, build_user_key
from session import session_runtime_uid, session_scoped_key
from utils import cli_proxy_artifact_path


def _build_init_data(bot_token: str, user_id: int) -> str:
    payload = {
        "auth_date": str(int(time.time())),
        "query_id": "q1",
        "user": json.dumps({"id": user_id, "username": f"u{user_id}", "first_name": "User"}, ensure_ascii=False),
    }
    check = "\n".join(f"{k}={v}" for k, v in sorted(payload.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    sig = hmac.new(secret, check.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"auth_date={payload['auth_date']}&query_id=q1&user={quote(payload['user'])}&hash={sig}"


def _suid(session) -> str:
    return session_runtime_uid(session)


def _build_config(tmp_path) -> AppConfig:
    cfg = AppConfig(
        telegram=TelegramConfig(
            token="t",
            whitelist_chat_ids=[2],
            admlist_chat_ids=[1],
            user_workdirs={2: [str(tmp_path)]},
        ),
        tools={"dummy": ToolConfig(name="dummy", mode="headless", cmd=["bash", "-lc", "cat"])},
        defaults=DefaultsConfig(
            workdir=str(tmp_path),
            state_path=str(tmp_path / "state.json"),
            toolhelp_path=str(tmp_path / "toolhelp.json"),
            log_path=str(tmp_path / "bot.log"),
        ),
        mcp=MCPConfig(enabled=False),
        mcp_clients=[],
        presets=[],
        path=str(tmp_path / "config.yaml"),
        miniapp=MiniAppConfig(enabled=True),
    )
    with open(cfg.path, "w", encoding="utf-8") as f:
        yaml.safe_dump(app_config_to_dict(cfg), f, sort_keys=False, allow_unicode=False)
    return cfg


class _FakeStream:
    def __init__(self, chunks, delay: float = 0.0):
        self._chunks = list(chunks)
        self._delay = delay

    async def read(self, _n: int) -> bytes:
        await asyncio.sleep(self._delay)
        if self._chunks:
            return self._chunks.pop(0)
        return b""


class _FakeProc:
    def __init__(self, stdout_chunks, stderr_chunks, *, wait_delay: float = 0.0, read_delay: float = 0.0):
        self.pid = 555555
        self.returncode = None
        self.stdin = None
        self.stdout = _FakeStream(stdout_chunks, read_delay)
        self.stderr = _FakeStream(stderr_chunks, read_delay)
        self._wait_delay = wait_delay

    async def wait(self) -> int:
        await asyncio.sleep(self._wait_delay)
        self.returncode = 0
        return 0

    async def communicate(self):
        out = bytearray()
        err = bytearray()
        while True:
            chunk = await self.stdout.read(4096)
            if not chunk:
                break
            out.extend(chunk)
        while True:
            chunk = await self.stderr.read(4096)
            if not chunk:
                break
            err.extend(chunk)
        self.returncode = 0
        return bytes(out), bytes(err)


class _FakeNonGitStatusSSH:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, int]] = []

    async def exec(self, workdir, host_alias, command, *, timeout_sec=30, chat_id=None):
        self.calls.append((str(workdir), str(host_alias), str(command), int(timeout_sec)))
        if "git rev-parse --is-inside-work-tree" in command:
            return SimpleNamespace(stdout="", stderr="", exit_code=128)
        return SimpleNamespace(stdout="ok\n", stderr="", exit_code=0)


class _FakeRemoteControlAuditSSH:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, int]] = []

    async def exec(self, workdir, host_alias, command, *, timeout_sec=30, chat_id=None):
        self.calls.append((str(workdir), str(host_alias), str(command), int(timeout_sec)))
        return SimpleNamespace(stdout="ok\n", stderr="", exit_code=0)


def test_logs_service_groups_multiline_and_filters_user_scope(tmp_path) -> None:
    cfg = _build_config(tmp_path)
    app = BotApp(cfg)
    session_user = app.manager.create(2, "dummy", str(tmp_path))
    session_admin = app.manager.create(1, "dummy", str(tmp_path))
    svc = LogsService(app)

    log_path = tmp_path / "bot.log"
    log_path.write_text(
        "\n".join(
            [
                f"2026-02-28 12:00:00,000 INFO [bot.run_prompt] [suid={_suid(session_user)}] run failed",
                "Traceback (most recent call last):",
                "  File \"x.py\", line 1, in <module>",
                "RuntimeError: boom",
                f"2026-02-28 12:00:01,000 INFO [bot.run_prompt] [suid={_suid(session_admin)}] secret",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    user_entries = svc.read_history(
        log_type="main",
        history_limit=100,
        user_id=2,
        is_admin=False,
    )
    assert len(user_entries) == 1
    assert "Traceback (most recent call last):" in user_entries[0]["text"]
    assert user_entries[0]["session_uid"] == _suid(session_user)

    admin_entries = svc.read_history(
        log_type="main",
        history_limit=100,
        user_id=1,
        is_admin=True,
    )
    assert len(admin_entries) == 2

    try:
        svc.ensure_session_scope_allowed(
            user_id=2,
            is_admin=False,
            session_uid=_suid(session_admin),
        )
        assert False, "expected LogAccessDeniedError"
    except LogAccessDeniedError:
        pass


def test_build_session_log_context_uses_canonical_session_uid(tmp_path) -> None:
    cfg = _build_config(tmp_path)
    app = BotApp(cfg)
    session = app.manager.create(2, "dummy", str(tmp_path))

    ctx = build_session_log_context(session=session, chat_id=2)

    assert ctx["session_uid"] == _suid(session)
    assert ctx["session_id"] == session.id
    assert ctx["chat_id"] == "2"


def test_logs_service_supports_compact_suid_context_format(tmp_path) -> None:
    cfg = _build_config(tmp_path)
    app = BotApp(cfg)
    session_user = app.manager.create(2, "dummy", str(tmp_path))
    session_admin = app.manager.create(1, "dummy", str(tmp_path))
    svc = LogsService(app)

    log_path = tmp_path / "bot.log"
    log_path.write_text(
        "\n".join(
            [
                f"2026-02-28 12:00:00,000 INFO [bot.run_prompt] [suid={_suid(session_user)}] run failed",
                f"2026-02-28 12:00:01,000 INFO [bot.run_prompt] [suid={_suid(session_admin)}] secret",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    user_entries = svc.read_history(
        log_type="main",
        history_limit=100,
        user_id=2,
        is_admin=False,
    )
    assert len(user_entries) == 1
    assert user_entries[0]["session_uid"] == _suid(session_user)
    assert "run failed" in user_entries[0]["text"]


def test_logs_service_hides_aiohttp_access_entries_from_miniapp_history(tmp_path) -> None:
    cfg = _build_config(tmp_path)
    app = BotApp(cfg)
    admin_session = app.manager.create(1, "dummy", str(tmp_path))
    svc = LogsService(app)

    log_path = tmp_path / "bot.log"
    log_path.write_text(
        "\n".join(
            [
                "2026-02-28 12:00:00,000 INFO [aiohttp.access] [sid=-] [suid=-] 127.0.0.1 "
                "\"GET /cli-proxy/ HTTP/1.1\" 200 123 \"-\" \"Mozilla/5.0\"",
                (
                    f"2026-02-28 12:00:01,000 INFO [bot.run_prompt] "
                    f"[sid={admin_session.id}] [suid={_suid(admin_session)}] visible line"
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    admin_entries = svc.read_history(
        log_type="main",
        history_limit=100,
        user_id=1,
        is_admin=True,
    )

    assert len(admin_entries) == 1
    assert admin_entries[0]["logger_name"] == "bot.run_prompt"
    assert "visible line" in admin_entries[0]["text"]


def test_logs_service_entry_allowed_rejects_aiohttp_access_entries(tmp_path) -> None:
    cfg = _build_config(tmp_path)
    app = BotApp(cfg)
    svc = LogsService(app)

    allowed = svc.entry_allowed(
        ParsedLogEntry(
            lines=["2026-02-28 12:00:00,000 INFO [aiohttp.access] [sid=-] [suid=-] noise"],
            logger_name="aiohttp.access",
            level="INFO",
            timestamp="2026-02-28 12:00:00,000",
        ),
        user_id=1,
        is_admin=True,
        session_uid_filter=None,
        session_id_filter=None,
        allowed_session_uids=set(),
        allowed_session_pairs=set(),
    )

    assert allowed is False


def test_logs_service_supports_per_session_filter_for_shared_canonical_scope(tmp_path) -> None:
    cfg = _build_config(tmp_path)
    app = BotApp(cfg)
    session_one = app.manager.create(2, "dummy", str(tmp_path))
    session_two = app.manager.create(2, "dummy", str(tmp_path))
    svc = LogsService(app)

    assert _suid(session_one) != _suid(session_two)

    log_path = tmp_path / "bot.log"
    log_path.write_text(
        "\n".join(
            [
                (
                    f"2026-02-28 12:00:00,000 INFO [session.headless] "
                    f"[sid={session_one.id}] [suid={_suid(session_one)}] line one"
                ),
                (
                    f"2026-02-28 12:00:01,000 INFO [session.headless] "
                    f"[sid={session_two.id}] [suid={_suid(session_two)}] line two"
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    sessions = svc.list_session_filters(user_id=2, is_admin=False)
    assert len(sessions) == 2
    assert {str(item["session_id"]) for item in sessions} == {session_one.id, session_two.id}
    assert {str(item["session_uid"]) for item in sessions} == {_suid(session_one), _suid(session_two)}

    filtered = svc.read_history(
        log_type="main",
        history_limit=100,
        user_id=2,
        is_admin=False,
        session_uid_filter=_suid(session_one),
        session_id_filter=session_one.id,
    )
    assert len(filtered) == 1
    assert filtered[0]["session_id"] == session_one.id
    assert "line one" in filtered[0]["text"]

    all_entries = svc.read_history(
        log_type="main",
        history_limit=100,
        user_id=2,
        is_admin=False,
    )
    assert len(all_entries) == 2
    assert {str(item["session_id"]) for item in all_entries} == {session_one.id, session_two.id}


def test_logs_service_ignores_legacy_context_format(tmp_path) -> None:
    cfg = _build_config(tmp_path)
    app = BotApp(cfg)
    app.manager.create(2, "dummy", str(tmp_path))
    svc = LogsService(app)

    log_path = tmp_path / "bot.log"
    log_path.write_text(
        "2026-02-28 12:00:00,000 INFO [bot.run_prompt] [chat=2 sid=s1 suid=2:s1 sname=User Session] legacy line\n",
        encoding="utf-8",
    )

    user_entries = svc.read_history(
        log_type="main",
        history_limit=100,
        user_id=2,
        is_admin=False,
    )
    assert user_entries == []


def test_logs_service_rejects_legacy_session_uid_alias_filter(tmp_path) -> None:
    cfg = _build_config(tmp_path)
    app = BotApp(cfg)
    session = app.manager.create(2, "dummy", str(tmp_path))
    svc = LogsService(app)
    legacy_filter_token = str(session.id)

    try:
        svc.ensure_session_scope_allowed(
            user_id=2,
            is_admin=False,
            session_uid=legacy_filter_token,
        )
        assert False, "expected LogAccessDeniedError"
    except LogAccessDeniedError:
        pass


def test_miniapp_logs_meta_and_ws_snapshot_scope(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path)
        app = BotApp(cfg)
        admin_session = app.manager.create(1, "dummy", str(tmp_path), message_thread_id=11)
        user_session = app.manager.create(2, "dummy", str(tmp_path), message_thread_id=77)

        # Запишем готовые строки в основной лог для истории.
        (tmp_path / "bot.log").write_text(
            "\n".join(
                [
                    f"2026-02-28 13:00:00,000 INFO [bot.run_prompt] [suid={_suid(user_session)}] user line",
                    "2026-02-28 13:00:00,500 INFO [bot.run_prompt] [suid=thread:2:99] archived line",
                    f"2026-02-28 13:00:01,000 INFO [bot.run_prompt] [suid={_suid(admin_session)}] admin line",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        web_app = web.Application()
        MiniAppRoutes(app).register(web_app)
        server = TestServer(web_app)
        await server.start_server()
        client = TestClient(server)
        await client.start_server()
        try:
            user_headers = {"X-Telegram-Init-Data": _build_init_data("t", 2)}
            admin_headers = {"X-Telegram-Init-Data": _build_init_data("t", 1)}

            resp_user = await client.get("/api/logs/meta", headers=user_headers)
            assert resp_user.status == 200
            body_user = await resp_user.json()
            assert body_user["is_admin"] is False
            assert [item["session_uid"] for item in body_user["sessions"]] == [_suid(user_session)]
            assert body_user["sessions"][0]["label"] == f"{user_session.id} | {user_session.name}"

            resp_admin = await client.get("/api/logs/meta", headers=admin_headers)
            assert resp_admin.status == 200
            body_admin = await resp_admin.json()
            assert body_admin["is_admin"] is True
            assert any(str(item["session_uid"]) == _suid(admin_session) for item in body_admin["sessions"])
            assert any(str(item["session_uid"]) == _suid(user_session) for item in body_admin["sessions"])
            assert not any(str(item["session_uid"]) == "thread:2:99" for item in body_admin["sessions"])
            admin_labels = {
                str(item["session_uid"]): str(item["label"])
                for item in body_admin["sessions"]
            }
            assert (
                admin_labels[_suid(admin_session)]
                == f"{admin_session.id} | {admin_session.name} | tg:1"
            )
            assert (
                admin_labels[_suid(user_session)]
                == f"{user_session.id} | {user_session.name} | tg:2"
            )

            ticket_resp = await client.get("/api/logs/ws_ticket", headers=user_headers)
            assert ticket_resp.status == 200
            ticket_body = await ticket_resp.json()
            ticket = str(ticket_body.get("ticket") or "")
            assert ticket

            denied = await client.get(
                f"/api/logs/ws?ticket={quote(ticket)}&log_type=main&history=100"
                f"&session_uid={_suid(admin_session)}"
            )
            assert denied.status == 403

            ticket_resp_2 = await client.get("/api/logs/ws_ticket", headers=user_headers)
            assert ticket_resp_2.status == 200
            ticket_2 = str((await ticket_resp_2.json()).get("ticket") or "")
            assert ticket_2

            ws = await client.ws_connect(f"/api/logs/ws?ticket={quote(ticket_2)}&log_type=main&history=100")
            try:
                first = await ws.receive_json(timeout=2)
                assert first["type"] == "snapshot"
                entries = first.get("entries", [])
                assert len(entries) == 1
                assert [item.get("session_uid") for item in entries] == [_suid(user_session)]
                assert any("user line" in str(item.get("text", "")) for item in entries)
                assert not any("archived line" in str(item.get("text", "")) for item in entries)
            finally:
                await ws.close()
        finally:
            await client.close()
            await server.close()

    asyncio.run(_run())


def test_remote_control_audit_entries_stay_local_and_follow_owner_admin_visibility(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path)
        app = BotApp(cfg)
        app.ssh_service = _FakeRemoteControlAuditSSH()
        admin_session = app.manager.create(1, "dummy", str(tmp_path))
        user_session = app.manager.create(2, "dummy", str(tmp_path))
        admin_session.modes.ssh_remote_enabled = True
        user_session.modes.ssh_remote_enabled = True

        save_ssh_config(str(tmp_path), {
            "alpha": SSHHostConfig(
                host="10.0.0.1",
                user="deploy",
                remote_project_root="/srv/alpha",
            ),
        })

        web_app = web.Application()
        MiniAppRoutes(app).register(web_app)
        server = TestServer(web_app)
        await server.start_server()
        client = TestClient(server)
        await client.start_server()
        try:
            user_headers = {"X-Telegram-Init-Data": _build_init_data("t", 2)}
            admin_headers = {"X-Telegram-Init-Data": _build_init_data("t", 1)}

            resp = await client.put(
                f"/api/session/{_suid(user_session)}/settings",
                headers=user_headers,
                json={
                    "remote_control_host_alias": "alpha",
                    "remote_control_enabled": True,
                },
            )
            assert resp.status == 200
            assert (await resp.json())["ok"] is True

            resp = await client.put(
                f"/api/session/{_suid(admin_session)}/settings",
                headers=admin_headers,
                json={
                    "remote_control_host_alias": "alpha",
                    "remote_control_enabled": True,
                },
            )
            assert resp.status == 200
            assert (await resp.json())["ok"] is True
        finally:
            await client.close()
            await server.close()

        for handler in list(logging.getLogger("miniapp").handlers):
            try:
                handler.flush()
            except Exception:
                pass

        log_path = tmp_path / "bot_miniapp.log"
        assert log_path.exists()
        log_text = log_path.read_text(encoding="utf-8")
        assert "remote_control_enabled" in log_text
        assert _suid(user_session) in log_text
        assert _suid(admin_session) in log_text

        svc = LogsService(app)
        user_entries = svc.read_history(
            log_type="miniapp",
            history_limit=100,
            user_id=2,
            is_admin=False,
        )
        assert user_entries
        assert {str(item["session_uid"]) for item in user_entries} == {_suid(user_session)}
        assert any("remote_control_enabled" in str(item["text"]) for item in user_entries)

        admin_entries = svc.read_history(
            log_type="miniapp",
            history_limit=100,
            user_id=1,
            is_admin=True,
        )
        assert any(str(item["session_uid"]) == _suid(user_session) for item in admin_entries)
        assert any(str(item["session_uid"]) == _suid(admin_session) for item in admin_entries)

    asyncio.run(_run())


def test_miniapp_logs_ws_supports_per_session_filter_inside_direct_chat_scope(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path)
        app = BotApp(cfg)
        session_one = app.manager.create(2, "dummy", str(tmp_path))
        session_two = app.manager.create(2, "dummy", str(tmp_path))

        (tmp_path / "bot.log").write_text(
            "\n".join(
                [
                    (
                        f"2026-02-28 13:00:00,000 INFO [session.headless] "
                        f"[sid={session_one.id}] [suid={_suid(session_one)}] line one"
                    ),
                    (
                        f"2026-02-28 13:00:01,000 INFO [session.headless] "
                        f"[sid={session_two.id}] [suid={_suid(session_two)}] line two"
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        web_app = web.Application()
        MiniAppRoutes(app).register(web_app)
        server = TestServer(web_app)
        await server.start_server()
        client = TestClient(server)
        await client.start_server()
        try:
            user_headers = {"X-Telegram-Init-Data": _build_init_data("t", 2)}
            ticket_resp = await client.get("/api/logs/ws_ticket", headers=user_headers)
            assert ticket_resp.status == 200
            ticket = str((await ticket_resp.json()).get("ticket") or "")
            assert ticket

            ws = await client.ws_connect(
                f"/api/logs/ws?ticket={quote(ticket)}&log_type=main&history=100"
                f"&session_uid={_suid(session_one)}&session_id={session_one.id}"
            )
            try:
                snapshot = await ws.receive_json(timeout=2)
                assert snapshot["type"] == "snapshot"
                entries = snapshot.get("entries", [])
                assert len(entries) == 1
                assert entries[0]["session_id"] == session_one.id
                assert "line one" in str(entries[0]["text"])
            finally:
                await ws.close()
        finally:
            await client.close()
            await server.close()

    asyncio.run(_run())


def test_miniapp_logs_ws_keeps_connection_with_short_heartbeat(tmp_path, monkeypatch) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path)
        app = BotApp(cfg)
        session = app.manager.create(1, "dummy", str(tmp_path))

        (tmp_path / "bot.log").write_text(
            f"2026-02-28 13:00:00,000 INFO [bot.run_prompt] [sid=s1] [suid={_suid(session)}] line\n",
            encoding="utf-8",
        )

        import miniapp.routes as routes_mod

        original_ws_response = routes_mod.web.WebSocketResponse

        def _short_heartbeat_ws_response(*args, **kwargs):
            kwargs["heartbeat"] = 0.2
            return original_ws_response(*args, **kwargs)

        monkeypatch.setattr(routes_mod.web, "WebSocketResponse", _short_heartbeat_ws_response)

        web_app = web.Application()
        MiniAppRoutes(app).register(web_app)
        server = TestServer(web_app)
        await server.start_server()
        client = TestClient(server)
        await client.start_server()
        try:
            headers = {"X-Telegram-Init-Data": _build_init_data("t", 1)}
            ticket_resp = await client.get("/api/logs/ws_ticket", headers=headers)
            assert ticket_resp.status == 200
            ticket = str((await ticket_resp.json()).get("ticket") or "")
            assert ticket

            ws = await client.ws_connect(f"/api/logs/ws?ticket={quote(ticket)}&log_type=main&history=100")
            try:
                snapshot = await ws.receive_json(timeout=2)
                assert snapshot["type"] == "snapshot"
                await asyncio.sleep(0.8)
                assert ws.closed is False
            finally:
                await ws.close()
        finally:
            await client.close()
            await server.close()

    asyncio.run(_run())


def test_miniapp_logs_download_respects_scope_and_filters(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path)
        app = BotApp(cfg)
        admin_session = app.manager.create(1, "dummy", str(tmp_path), message_thread_id=11)
        user_session = app.manager.create(2, "dummy", str(tmp_path), message_thread_id=77)

        (tmp_path / "bot.log").write_text(
            "\n".join(
                [
                    f"2026-02-28 13:00:00,000 INFO [bot.run_prompt] [suid={_suid(user_session)}] user line 1",
                    "2026-02-28 13:00:00,500 INFO [bot.run_prompt] [suid=thread:2:88] user line 2",
                    f"2026-02-28 13:00:01,000 INFO [bot.run_prompt] [suid={_suid(admin_session)}] admin line",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        web_app = web.Application()
        MiniAppRoutes(app).register(web_app)
        server = TestServer(web_app)
        await server.start_server()
        client = TestClient(server)
        await client.start_server()
        try:
            user_headers = {"X-Telegram-Init-Data": _build_init_data("t", 2)}

            ticket_resp = await client.get("/api/logs/ws_ticket", headers=user_headers)
            assert ticket_resp.status == 200
            ticket = str((await ticket_resp.json()).get("ticket") or "")
            assert ticket

            denied = await client.get(
                f"/api/logs/download?ticket={quote(ticket)}&log_type=main&history=100"
                f"&session_uid={_suid(admin_session)}"
            )
            assert denied.status == 403

            resp = await client.get(
                f"/api/logs/download?ticket={quote(ticket)}&log_type=main&history=100"
                f"&session_uid={_suid(user_session)}"
            )
            assert resp.status == 200
            assert resp.headers.get("Content-Disposition", "").startswith("attachment; filename=\"miniapp-main-")
            text = await resp.text()
            assert "user line 1" in text
            assert "user line 2" not in text
            assert "admin line" not in text
        finally:
            await client.close()
            await server.close()

    asyncio.run(_run())


def test_miniapp_status_ws_snapshot_contains_selected_session_fields_for_explicit_session_uid(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path)
        app = BotApp(cfg)
        session = app.manager.create(2, "dummy", str(tmp_path))
        session.busy = True
        session.modes.active_mode = "manager"
        session.executor_profile = "analyst"
        session.last_tick_value = "tick-42"
        session.state_summary = "S" * 640
        save_plan(
            str(tmp_path),
            ProjectPlan(
                project_goal="Miniapp manager status",
                status="active",
                tasks=[
                    DevTask(
                        id="task_1",
                        title="Собрать статус",
                        description="Показать план в miniapp",
                        acceptance_criteria=["Есть отдельный аккордеон с планом"],
                        status="in_progress",
                        attempt=1,
                        max_attempts=3,
                    )
                ],
                current_task_id="task_1",
            ),
        )
        append_session_tick(session, value="tick-41", ts=1_772_279_997.0)
        append_session_tick(session, value="tick-42", ts=1_772_279_998.5)
        session.resume_token = "resume-abc-123"
        session.queue.append({"text": "queued input", "dest": {"kind": "telegram", "chat_id": 2}})

        web_app = web.Application()
        MiniAppRoutes(app).register(web_app)
        server = TestServer(web_app)
        await server.start_server()
        client = TestClient(server)
        await client.start_server()
        try:
            headers = {"X-Telegram-Init-Data": _build_init_data("t", 2)}
            ticket_resp = await client.get("/api/status/ws_ticket", headers=headers)
            assert ticket_resp.status == 200
            ticket = str((await ticket_resp.json()).get("ticket") or "")
            assert ticket

            ws = await client.ws_connect(
                f"/api/status/ws?ticket={quote(ticket)}&session_uid={_suid(session)}"
            )
            try:
                first = await ws.receive_json(timeout=2)
                assert first["type"] == "snapshot"
                status = first.get("status", {})
                assert status.get("user", {}).get("user_id") == 2
                assert status.get("session_count") == 1
                assert status.get("selected_session_uid") == _suid(session)
                assert status.get("available_sessions", [])[0].get("label") == f"{session.id} | {session.name}"
                active = status.get("active_session", {})
                assert active.get("session_uid") == _suid(session)
                assert active.get("id") == session.id
                assert active.get("display_title") == f"{session.id} | {session.name}"
                assert active.get("busy") is True
                assert active.get("active_mode") == "manager"
                assert active.get("active_cli") == "dummy"
                assert active.get("queue_len") == 1
                assert active.get("active_resume_token") == "resume-abc-123"
                assert active.get("resume_token_present") is True
                manager_plan_status = str(active.get("manager_plan_status") or "")
                assert "План: 0/1 задач выполнено. Статус: active." in manager_plan_status
                assert "Собрать статус [in_progress]" in manager_plan_status
                assert active.get("state_summary") == "S" * 640
                assert active.get("tick_history") == [
                    {"ts": 1_772_279_997.0, "value": "tick-41"},
                    {"ts": 1_772_279_998.5, "value": "tick-42"},
                ]
                assert "Активная сессия" in str(active.get("status_text", ""))
                fields = active.get("fields", {})
                assert isinstance(fields, dict)
                assert fields.get("busy") is True
                assert fields.get("active_mode") == "manager"
                assert fields.get("state_summary") == "S" * 640
            finally:
                await ws.close()
        finally:
            await client.close()
            await server.close()

    asyncio.run(_run())


def test_miniapp_status_ws_marks_remote_non_git_target_as_unavailable(tmp_path) -> None:
    async def _run() -> None:
        workdir = tmp_path / "plain-remote"
        workdir.mkdir()

        cfg = _build_config(tmp_path)
        app = BotApp(cfg)
        app.ssh_service = _FakeNonGitStatusSSH()
        session = app.manager.create(2, "dummy", str(workdir))
        session.modes.ssh_remote_enabled = True
        session.modes.remote_control_enabled = True
        session.modes.remote_control_host_alias = "plain"

        save_ssh_config(str(workdir), {
            "plain": SSHHostConfig(
                host="1.1.1.1",
                user="deploy",
                remote_project_root="/srv/plain",
            ),
        })

        web_app = web.Application()
        MiniAppRoutes(app).register(web_app)
        server = TestServer(web_app)
        await server.start_server()
        client = TestClient(server)
        await client.start_server()
        try:
            headers = {"X-Telegram-Init-Data": _build_init_data("t", 2)}
            ticket_resp = await client.get("/api/status/ws_ticket", headers=headers)
            assert ticket_resp.status == 200
            ticket = str((await ticket_resp.json()).get("ticket") or "")
            assert ticket

            ws = await client.ws_connect(
                f"/api/status/ws?ticket={quote(ticket)}&session_uid={_suid(session)}"
            )
            try:
                first = await ws.receive_json(timeout=2)
                assert first["type"] == "snapshot"
                active = first.get("status", {}).get("active_session", {}) or {}
                assert active.get("execution_target") == "remote"
                assert active.get("remote_host_alias") == "plain"
                assert active.get("remote_project_root") == "/srv/plain"
                assert active.get("git_available") is False
                assert active.get("git_busy") is False
                assert active.get("git_conflict") is False
            finally:
                await ws.close()
        finally:
            await client.close()
            await server.close()

        assert app.ssh_service.calls == [
            (
                str(workdir),
                "plain",
                "cd '/srv/plain' && git rev-parse --is-inside-work-tree 2>/dev/null",
                10,
            ),
        ]

    asyncio.run(_run())


def test_miniapp_status_ws_snapshot_exposes_last_assistant_text_and_tick_kinds(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path)
        app = BotApp(cfg)
        session = app.manager.create(2, "dummy", str(tmp_path))
        append_session_tick(session, value="tool tick", ts=1_772_279_997.0, kind="tool_event")
        append_session_tick(session, value="assistant final", ts=1_772_279_998.5, kind="assistant_text")

        web_app = web.Application()
        MiniAppRoutes(app).register(web_app)
        server = TestServer(web_app)
        await server.start_server()
        client = TestClient(server)
        await client.start_server()
        try:
            headers = {"X-Telegram-Init-Data": _build_init_data("t", 2)}
            ticket_resp = await client.get("/api/status/ws_ticket", headers=headers)
            assert ticket_resp.status == 200
            ticket = str((await ticket_resp.json()).get("ticket") or "")
            assert ticket

            ws = await client.ws_connect(
                f"/api/status/ws?ticket={quote(ticket)}&session_uid={_suid(session)}"
            )
            try:
                first = await ws.receive_json(timeout=2)
                assert first["type"] == "snapshot"
                active = first.get("status", {}).get("active_session", {}) or {}
                assert active.get("last_assistant_text_value") == "assistant final"
                assert active.get("assistant_tick_count") == 1
                assert active.get("tick_history") == [
                    {"ts": 1_772_279_997.0, "value": "tool tick", "kind": "tool_event"},
                    {"ts": 1_772_279_998.5, "value": "assistant final", "kind": "assistant_text"},
                ]
            finally:
                await ws.close()
        finally:
            await client.close()
            await server.close()

    asyncio.run(_run())


def test_miniapp_status_ws_streams_gemini_ticks_while_session_runs(tmp_path, monkeypatch) -> None:
    async def _run() -> None:
        workdir = tmp_path / "repo"
        workdir.mkdir()

        cfg = _build_config(tmp_path)
        cfg.tools["gemini"] = ToolConfig(
            name="gemini",
            mode="headless",
            cmd=["gemini", "--approval-mode", "yolo", "--resume", "latest", "-p", "{prompt}"],
            headless_cmd=["gemini", "--approval-mode", "yolo", "--resume", "latest", "-p", "{prompt}"],
            separate_stderr=False,
        )
        app = BotApp(cfg)
        session = app.manager.create(2, "gemini", str(workdir))
        session.busy = True
        session.started_at = time.time()
        session.last_output_ts = session.started_at

        async def _fake_create_subprocess_exec(*args, **_kwargs):
            return _FakeProc(
                stdout_chunks=[
                    b'{"type":"init","session_id":"ba568ec1-3d9d-424d-86cc-55644c4124d7"}\n',
                    b'{"type":"message","role":"assistant","content":"Analyzing Command Execution","delta":true}\n',
                    (
                        b'{"type":"tool_use","tool_name":"run_shell_command","tool_id":"tool-1",'
                        b'"parameters":{"command":"printf \\"tick-1\\\\ntick-2\\\\ntick-3\\""}}\n'
                    ),
                    b'{"type":"tool_result","tool_id":"tool-1","status":"success","output":"tick-1\\ntick-2\\ntick-3"}\n',
                    b'{"type":"message","role":"assistant","content":"Final answer from stdout","delta":false}\n',
                    b'{"type":"result","status":"success"}\n',
                ],
                stderr_chunks=[
                    b"YOLO mode is enabled. All tool calls will be automatically approved.\n",
                    b"Loaded cached credentials.\n",
                ],
                wait_delay=0.65,
                read_delay=0.08,
            )

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)

        web_app = web.Application()
        MiniAppRoutes(app).register(web_app)
        server = TestServer(web_app)
        await server.start_server()
        client = TestClient(server)
        await client.start_server()
        try:
            headers = {"X-Telegram-Init-Data": _build_init_data("t", 2)}
            ticket_resp = await client.get("/api/status/ws_ticket", headers=headers)
            assert ticket_resp.status == 200
            ticket = str((await ticket_resp.json()).get("ticket") or "")
            assert ticket

            ws = await client.ws_connect(
                f"/api/status/ws?ticket={quote(ticket)}&session_uid={_suid(session)}"
            )
            run_task = asyncio.create_task(session._run_headless("hello"))
            try:
                saw_progress = False
                saw_final_assistant = False
                final_active = {}
                for _ in range(12):
                    payload = await ws.receive_json(timeout=2.0)
                    assert payload["type"] in {"snapshot", "update"}
                    final_active = payload.get("status", {}).get("active_session", {}) or {}
                    history = list(final_active.get("tick_history") or [])
                    if history and any("tick-1 tick-2 tick-3" in str(item.get("value")) for item in history):
                        saw_progress = True
                    if final_active.get("last_assistant_text_value") == "Final answer from stdout":
                        saw_final_assistant = True
                    if saw_progress and saw_final_assistant:
                        break
                assert saw_progress is True
                assert saw_final_assistant is True
                assert final_active.get("last_assistant_text_value") == "Final answer from stdout"
                assert any(
                    "Analyzing Command Execution" in str(item.get("value"))
                    for item in list(final_active.get("tick_history") or [])
                )
                assert any(
                    "tick-1 tick-2 tick-3" in str(item.get("value"))
                    for item in list(final_active.get("tick_history") or [])
                )
            finally:
                await ws.close()
            assert await run_task == "Final answer from stdout"
        finally:
            session.busy = False
            await client.close()
            await server.close()

    asyncio.run(_run())


def test_miniapp_status_ws_without_session_uid_does_not_auto_select_first_session(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path)
        app = BotApp(cfg)
        session = app.manager.create(2, "dummy", str(tmp_path))

        web_app = web.Application()
        MiniAppRoutes(app).register(web_app)
        server = TestServer(web_app)
        await server.start_server()
        client = TestClient(server)
        await client.start_server()
        try:
            headers = {"X-Telegram-Init-Data": _build_init_data("t", 2)}
            ticket_resp = await client.get("/api/status/ws_ticket", headers=headers)
            assert ticket_resp.status == 200
            ticket = str((await ticket_resp.json()).get("ticket") or "")
            assert ticket

            ws = await client.ws_connect(f"/api/status/ws?ticket={quote(ticket)}")
            try:
                first = await ws.receive_json(timeout=2)
                assert first["type"] == "snapshot"
                status = first.get("status", {})
                assert status.get("session_count") == 1
                assert status.get("selected_session_uid") == ""
                assert status.get("active_session") is None
                assert status.get("status_text") == "Сессия не выбрана"
                available = status.get("available_sessions", [])
                assert [item.get("session_uid") for item in available] == [_suid(session)]
            finally:
                await ws.close()
        finally:
            await client.close()
            await server.close()

    asyncio.run(_run())


def test_miniapp_status_ws_refreshes_available_sessions_after_session_inventory_change(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path)
        app = BotApp(cfg)
        first_session = app.manager.create(2, "dummy", str(tmp_path / "first"))

        web_app = web.Application()
        MiniAppRoutes(app).register(web_app)
        server = TestServer(web_app)
        await server.start_server()
        client = TestClient(server)
        await client.start_server()
        try:
            headers = {"X-Telegram-Init-Data": _build_init_data("t", 2)}
            ticket_resp = await client.get("/api/status/ws_ticket", headers=headers)
            assert ticket_resp.status == 200
            ticket = str((await ticket_resp.json()).get("ticket") or "")
            assert ticket

            ws = await client.ws_connect(f"/api/status/ws?ticket={quote(ticket)}")
            try:
                first = await ws.receive_json(timeout=2)
                assert first["type"] == "snapshot"
                first_status = first.get("status", {})
                assert [item.get("session_uid") for item in first_status.get("available_sessions", [])] == [_suid(first_session)]

                second_session = app.manager.create(2, "dummy", str(tmp_path / "second"))

                for _ in range(4):
                    payload = await ws.receive_json(timeout=2)
                    assert payload["type"] == "update"
                    available = payload.get("status", {}).get("available_sessions", [])
                    session_uids = [item.get("session_uid") for item in available]
                    if _suid(second_session) in session_uids:
                        assert session_uids == [_suid(first_session), _suid(second_session)]
                        break
                else:
                    raise AssertionError("MiniApp status websocket did not refresh available_sessions after creating a new session")
            finally:
                await ws.close()
        finally:
            await client.close()
            await server.close()

    asyncio.run(_run())


def test_miniapp_status_payload_includes_analyst_mode_status(tmp_path) -> None:
    cfg = _build_config(tmp_path)
    app = BotApp(cfg)
    session = app.manager.create(2, "dummy", str(tmp_path))
    session.modes.active_mode = "analyst"

    store = AnalystStateStore(cli_proxy_artifact_path(str(tmp_path), ".analyst_data"))
    ctx = store.load(build_context_key(2, session.id))
    ctx.mode = "audit"
    store.save(ctx)
    app.ui_state.pending_questions = {
        "q1": {
            "session_id": session.id,
            "awaiting_custom": False,
        }
    }

    payload = MiniAppRoutes(app)._build_session_payload(session, session_chat_id=2)
    text = str(payload.get("analyst_mode_status") or "")
    assert "🧠 Статус Аналитика" in text
    assert "Режим: включен" in text
    details = payload.get("analyst_mode_status_details") or {}
    assert isinstance(details, dict)
    assert "pending_questions" in details
    assert "active_plugin_flow" in details
    assert "template" in details
    assert "queue_origin" in details


def test_miniapp_status_payload_includes_agent_mode_status_details(tmp_path) -> None:
    cfg = _build_config(tmp_path)
    app = BotApp(cfg)
    session = app.manager.create(2, "dummy", str(tmp_path))
    session.modes.active_mode = "agent"
    session.queue.append({"text": "queued task", "dest": {"kind": "telegram", "chat_id": 2, "user_id": 99}})
    app.ui_state.pending_questions = {
        "qa1": {
            "session_id": session.id,
            "chat_id": 2,
            "awaiting_custom": True,
            "created_at": 1_700_000_010.0,
        }
    }
    app.mode_agent_project_pending_by_chat.set(
        agent_project_scope_key(2),
        {
            "session_id": session.id,
            "session_scoped_key": session_scoped_key(session),
            "ui_chat_id": 2,
            "message_thread_id": None,
        },
    )
    emit_runtime_progress(
        session,
        {
            "mode_id": "agent",
            "source": "agent_core",
            "phase": "iteration",
            "status": "running",
            "message": "Итерация 1: вызов инструмента",
        },
    )

    payload = MiniAppRoutes(app)._build_session_payload(session, session_chat_id=2)
    text = str(payload.get("agent_mode_status") or "")
    assert "🤖 Статус Агента" in text
    details = payload.get("agent_mode_status_details") or {}
    assert isinstance(details, dict)
    assert "pending_questions" in details
    assert "active_plugin_flow" in details
    assert "runtime_progress" in details
    assert "template" in details
    assert "queue_origin" in details
    runtime = payload.get("runtime_progress") or {}
    assert isinstance(runtime, dict)
    assert runtime.get("last_source") == "agent_core"
    assert runtime.get("last_phase") == "iteration"
    recent = runtime.get("recent_events") or []
    assert isinstance(recent, list)
    assert isinstance(recent[-1], dict)
    assert recent[-1].get("source") == "agent_core"


def test_miniapp_status_payload_exposes_runtime_progress_for_non_agent_mode(tmp_path) -> None:
    cfg = _build_config(tmp_path)
    app = BotApp(cfg)
    session = app.manager.create(2, "dummy", str(tmp_path))
    session.modes.active_mode = "analyst"
    emit_runtime_progress(
        session,
        {
            "mode_id": "analyst",
            "source": "agent_core",
            "phase": "tool_batch",
            "status": "running",
            "message": "Вызовы инструментов",
        },
    )

    payload = MiniAppRoutes(app)._build_session_payload(session, session_chat_id=2)
    runtime = payload.get("runtime_progress") or {}
    assert isinstance(runtime, dict)
    assert runtime.get("last_source") == "agent_core"
    assert runtime.get("last_phase") == "tool_batch"
    recent = runtime.get("recent_events") or []
    assert isinstance(recent, list)
    assert isinstance(recent[-1], dict)
    assert recent[-1].get("phase") == "tool_batch"
    assert "agent_core/tool_batch/running" in str(payload.get("runtime_status") or "")


def test_miniapp_status_payload_includes_webmaster_mode_status(tmp_path) -> None:
    cfg = _build_config(tmp_path)
    app = BotApp(cfg)
    session = app.manager.create(2, "dummy", str(tmp_path))
    session.modes.active_mode = "webmaster"

    store = WebmasterStateStore(cli_proxy_artifact_path(str(tmp_path), ".webmaster_data"))
    key = build_user_key(2, 2, session.id)
    wm_ctx = store.reset(key)
    wm_ctx.stage = "await_intent_update"
    wm_ctx.task_kind = "continue_task"
    wm_ctx.active_prompt_version = 3
    wm_ctx.last_feedback_class = "new_task"
    store.save(wm_ctx)

    payload = MiniAppRoutes(app)._build_session_payload(session, session_chat_id=2)
    text = str(payload.get("webmaster_mode_status") or "")
    assert "🌐 Статус Вебмастера" in text
    assert "Тип задачи: continue_task" in text
    assert "Версия промпта: 3" in text
    assert "Последняя классификация: new_task" in text


def test_miniapp_status_ws_invalid_ticket_is_rejected(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path)
        app = BotApp(cfg)
        app.manager.create(2, "dummy", str(tmp_path))

        web_app = web.Application()
        MiniAppRoutes(app).register(web_app)
        server = TestServer(web_app)
        await server.start_server()
        client = TestClient(server)
        await client.start_server()
        try:
            resp = await client.get("/api/status/ws?ticket=invalid-ticket")
            assert resp.status == 401
        finally:
            await client.close()
            await server.close()

    asyncio.run(_run())


def test_miniapp_status_ws_user_cannot_select_foreign_session(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path)
        app = BotApp(cfg)
        foreign = app.manager.create(1, "dummy", str(tmp_path))
        app.manager.create(2, "dummy", str(tmp_path))

        web_app = web.Application()
        MiniAppRoutes(app).register(web_app)
        server = TestServer(web_app)
        await server.start_server()
        client = TestClient(server)
        await client.start_server()
        try:
            user_headers = {"X-Telegram-Init-Data": _build_init_data("t", 2)}
            ticket_resp = await client.get("/api/status/ws_ticket", headers=user_headers)
            assert ticket_resp.status == 200
            ticket = str((await ticket_resp.json()).get("ticket") or "")
            assert ticket

            denied = await client.get(
                f"/api/status/ws?ticket={quote(ticket)}&session_uid={_suid(foreign)}"
            )
            assert denied.status == 403
        finally:
            await client.close()
            await server.close()

    asyncio.run(_run())


def test_miniapp_status_ws_user_rejects_legacy_session_uid_alias_filter(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path)
        app = BotApp(cfg)
        own = app.manager.create(2, "dummy", str(tmp_path))

        web_app = web.Application()
        MiniAppRoutes(app).register(web_app)
        server = TestServer(web_app)
        await server.start_server()
        client = TestClient(server)
        await client.start_server()
        try:
            user_headers = {"X-Telegram-Init-Data": _build_init_data("t", 2)}
            ticket_resp = await client.get("/api/status/ws_ticket", headers=user_headers)
            assert ticket_resp.status == 200
            ticket = str((await ticket_resp.json()).get("ticket") or "")
            assert ticket

            denied = await client.get(
                f"/api/status/ws?ticket={quote(ticket)}&session_uid=2:{own.id}"
            )
            assert denied.status == 400
            body = await denied.json()
            assert (
                body["error"]
                == "session_uid chat_id:session_id format is not supported; use canonical session_uid"
            )
        finally:
            await client.close()
            await server.close()

    asyncio.run(_run())


def test_miniapp_status_ws_admin_can_select_any_session(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path)
        app = BotApp(cfg)
        app.manager.create(1, "dummy", str(tmp_path))
        target = app.manager.create(2, "dummy", str(tmp_path))

        web_app = web.Application()
        MiniAppRoutes(app).register(web_app)
        server = TestServer(web_app)
        await server.start_server()
        client = TestClient(server)
        await client.start_server()
        try:
            admin_headers = {"X-Telegram-Init-Data": _build_init_data("t", 1)}
            ticket_resp = await client.get("/api/status/ws_ticket", headers=admin_headers)
            assert ticket_resp.status == 200
            ticket = str((await ticket_resp.json()).get("ticket") or "")
            assert ticket

            ws = await client.ws_connect(
                f"/api/status/ws?ticket={quote(ticket)}&session_uid={_suid(target)}"
            )
            try:
                first = await ws.receive_json(timeout=2)
                assert first["type"] == "snapshot"
                status = first.get("status", {})
                assert status.get("selected_session_uid") == _suid(target)
                labels = {
                    str(item.get("session_uid")): str(item.get("label"))
                    for item in status.get("available_sessions", [])
                }
                assert labels[_suid(target)] == f"{target.id} | {target.name} | tg:2"
                active = status.get("active_session", {})
                assert active.get("id") == target.id
                assert active.get("workdir") == str(tmp_path)
                assert active.get("display_title") == f"{target.id} | {target.name} | tg:2"
            finally:
                await ws.close()
        finally:
            await client.close()
            await server.close()

    asyncio.run(_run())
