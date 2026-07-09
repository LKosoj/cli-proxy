import asyncio
import base64
import hashlib
import hmac
import json
import shlex
import time
from types import SimpleNamespace
from urllib.parse import quote
from unittest.mock import AsyncMock, patch

from aiohttp import ClientSession, web
from aiohttp.test_utils import TestClient, TestServer
import yaml

from app.services.ssh_config_loader import load_ssh_secrets, save_ssh_config
from app.services.ssh_service import SSHKeygenResult, SSHTestResult
from bot import BotApp
from app.services.shared_http_ingress import SharedHttpIngress
from config import AppConfig, DefaultsConfig, MCPConfig, MiniAppConfig, SSHHostConfig, TelegramConfig, ToolConfig
from miniapp.routes import MiniAppRoutes
from miniapp.server import MiniAppServer
from miniapp.services.config_service import app_config_to_dict
from modes.admin.schemas import AdminStatusPayloadSchema, validate_admin_payload
from modes.admin.state_store import AdminStateStore
from session import session_runtime_uid


def _build_init_data(bot_token: str, user_id: int = 1) -> str:
    payload = {
        "auth_date": str(int(time.time())),
        "query_id": "q1",
        "user": json.dumps({"id": user_id, "username": "admin", "first_name": "Admin"}, ensure_ascii=False),
    }
    check = "\n".join(f"{k}={v}" for k, v in sorted(payload.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    sig = hmac.new(secret, check.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"auth_date={payload['auth_date']}&query_id=q1&user={quote(payload['user'])}&hash={sig}"


def _build_config(tmp_path, *, token: str = "t") -> AppConfig:
    cfg = AppConfig(
        telegram=TelegramConfig(token=token, whitelist_chat_ids=[1], admlist_chat_ids=[1]),
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


def _register_routes(web_app: web.Application, app: BotApp) -> MiniAppRoutes:
    routes = MiniAppRoutes(app)
    routes.register(web_app)
    return routes


def _remote_path_token(token: str) -> str:
    path = str(token or "")
    if path.endswith("/*") or path.endswith("/.*"):
        return path.rsplit("/", 1)[0]
    return path


class _FakeRemoteConflictSSH:
    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = dict(files)
        self.calls: list[tuple[str, str, str]] = []

    async def exec(self, workdir, host_alias, command, *, timeout_sec=30, chat_id=None):
        self.calls.append((str(workdir), str(host_alias), str(command)))
        words = shlex.split(str(command))

        if len(words) >= 3 and words[:2] == ["readlink", "-f"]:
            return SimpleNamespace(stdout=f"{words[2]}\n", stderr="", exit_code=0)
        if len(words) >= 3 and words[:2] == ["test", "-L"] and "readlink" in words:
            return SimpleNamespace(stdout="NOT_LINK\n", stderr="", exit_code=0)
        if len(words) >= 3 and words[:2] == ["test", "-L"]:
            return SimpleNamespace(stdout="OK\n", stderr="", exit_code=0)
        if len(words) >= 3 and words[:2] == ["test", "-f"]:
            tag = "OK" if words[2] in self.files else "MISSING"
            return SimpleNamespace(stdout=f"{tag}\n", stderr="", exit_code=0)
        if len(words) >= 2 and words[0] == "sha256sum":
            target = words[1]
            if target not in self.files:
                return SimpleNamespace(stdout="", stderr="missing", exit_code=1)
            digest = hashlib.sha256(self.files[target]).hexdigest()
            return SimpleNamespace(stdout=f"{digest}  {target}\n", stderr="", exit_code=0)
        if len(words) >= 2 and words[0] == "base64":
            target = words[1]
            if target not in self.files:
                return SimpleNamespace(stdout="", stderr="missing", exit_code=1)
            encoded = base64.b64encode(self.files[target]).decode("ascii")
            return SimpleNamespace(stdout=f"{encoded}\n", stderr="", exit_code=0)
        if "base64 -d" in command and "mv" in command:
            return SimpleNamespace(stdout="", stderr="", exit_code=0)

        return SimpleNamespace(stdout="", stderr="", exit_code=0)


class _FakeRemoteShellSSH:
    def __init__(
        self,
        *,
        is_git: bool = True,
        search_output: str = "src/main.py:12:needle found remotely\n",
        files: dict[str, bytes] | None = None,
    ) -> None:
        self._is_git = bool(is_git)
        self._search_output = str(search_output)
        self._files = dict(files or {})
        self.calls: list[tuple[str, str, str, int]] = []

    async def exec(self, workdir, host_alias, command, *, timeout_sec=30, chat_id=None):
        self.calls.append((str(workdir), str(host_alias), str(command), int(timeout_sec)))
        words = shlex.split(str(command))

        if "git rev-parse --is-inside-work-tree" in command:
            if self._is_git:
                return SimpleNamespace(stdout="true\n", stderr="", exit_code=0)
            return SimpleNamespace(stdout="", stderr="", exit_code=128)
        if "git status --porcelain" in command:
            return SimpleNamespace(stdout="UU conflict.py\n", stderr="", exit_code=0)
        if "rg -n" in command or "grep -rn" in command:
            return SimpleNamespace(
                stdout=self._search_output,
                stderr="",
                exit_code=0,
            )
        if len(words) >= 3 and words[:2] == ["readlink", "-f"]:
            return SimpleNamespace(stdout=f"{words[2]}\n", stderr="", exit_code=0)
        if len(words) >= 3 and words[:2] == ["test", "-d"]:
            path = words[2]
            is_dir = any(file_path.startswith(f"{path}/") for file_path in self._files)
            tag = "DIR" if is_dir else "MISSING"
            return SimpleNamespace(stdout=f"{tag}\n", stderr="", exit_code=0)
        if len(words) >= 4 and words[:2] == ["stat", "-c"] and "%n" in words[2]:
            lines = []
            dir_paths = {
                _remote_path_token(token)
                for token in words[3:]
                if _remote_path_token(token).startswith("/srv/")
            }
            for dir_path in sorted(dir_paths):
                for file_path, raw in sorted(self._files.items()):
                    parent = file_path.rsplit("/", 1)[0] if "/" in file_path else ""
                    if parent == dir_path:
                        lines.append(f"regular file|{len(raw)}|1700000000|{file_path}")
            return SimpleNamespace(stdout="\n".join(lines) + ("\n" if lines else ""), stderr="", exit_code=0)
        if len(words) >= 3 and words[:2] == ["test", "-L"] and "readlink" in words:
            return SimpleNamespace(stdout="NOT_LINK\n", stderr="", exit_code=0)
        if len(words) >= 3 and words[:2] == ["test", "-L"]:
            return SimpleNamespace(stdout="OK\n", stderr="", exit_code=0)
        if len(words) >= 3 and words[:2] == ["test", "-f"]:
            path = words[2]
            tag = "OK" if path in self._files else "MISSING"
            return SimpleNamespace(stdout=f"{tag}\n", stderr="", exit_code=0)
        if len(words) >= 4 and words[:3] == ["stat", "-c", "%s %Y"]:
            path = words[3]
            raw = self._files.get(path)
            if raw is None:
                return SimpleNamespace(stdout="", stderr="missing", exit_code=1)
            return SimpleNamespace(stdout=f"{len(raw)} 1700000000\n", stderr="", exit_code=0)
        if len(words) >= 4 and words[:2] == ["stat", "-c"] and words[2] == "%F|%s|%Y":
            path = words[3]
            raw = self._files.get(path)
            if raw is None:
                return SimpleNamespace(stdout="", stderr="missing", exit_code=1)
            return SimpleNamespace(stdout=f"regular file|{len(raw)}|1700000000\n", stderr="", exit_code=0)
        if len(words) >= 2 and words[0] == "base64":
            path = words[1]
            raw = self._files.get(path)
            if raw is None:
                return SimpleNamespace(stdout="", stderr="missing", exit_code=1)
            encoded = base64.b64encode(raw).decode("ascii")
            return SimpleNamespace(stdout=f"{encoded}\n", stderr="", exit_code=0)

        return SimpleNamespace(stdout="ok\n", stderr="", exit_code=0)


class _FakeSshRouteService:
    def __init__(self) -> None:
        self.test_connection_calls: list[tuple[str, str]] = []
        self.generate_key_calls: list[tuple[str, str]] = []

    async def test_connection(self, workdir: str, alias: str) -> SSHTestResult:
        self.test_connection_calls.append((str(workdir), str(alias)))
        return SSHTestResult(ok=True, message="connection ok", server_info="Linux test-host")

    async def generate_key(self, workdir: str, alias: str) -> SSHKeygenResult:
        self.generate_key_calls.append((str(workdir), str(alias)))
        return SSHKeygenResult(
            private_path=f"{workdir}/.cli-proxy/ssh/{alias}",
            public_key_text=f"ssh-ed25519 AAAA{alias}",
        )


def test_miniapp_ssh_routes_preserve_contract_with_real_auth_path(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path, token="t")
        app = BotApp(cfg)
        app.ssh_service = _FakeSshRouteService()
        workdir = tmp_path / "ssh-contract"
        workdir.mkdir()

        web_app = web.Application()
        routes = MiniAppRoutes(app)
        routes.register(web_app)

        server = TestServer(web_app)
        await server.start_server()
        client = TestClient(server)
        await client.start_server()
        try:
            missing_auth = await client.get(f"/api/ssh/hosts?workdir={workdir}")
            assert missing_auth.status == 401

            headers = {"X-Telegram-Init-Data": _build_init_data("t", 1)}
            qs = f"?workdir={workdir}"

            list_resp = await client.get(f"/api/ssh/hosts{qs}", headers=headers)
            assert list_resp.status == 200
            assert await list_resp.json() == {"ok": True, "hosts": {}}

            create_resp = await client.post(
                f"/api/ssh/hosts{qs}",
                headers=headers,
                json={
                    "alias": "prod",
                    "host": "10.0.0.1",
                    "port": 2222,
                    "user": "deploy",
                    "auth": "password",
                    "password": "host-secret",
                    "sudo": True,
                    "sudo_password": "sudo-secret",
                    "allowed_chat_ids": [1],
                    "roles": ["web"],
                    "description": "Production",
                    "remote_project_root": "/srv/app",
                },
            )
            assert create_resp.status == 200
            assert await create_resp.json() == {"ok": True, "alias": "prod"}

            detail_resp = await client.get(f"/api/ssh/hosts/prod{qs}", headers=headers)
            assert detail_resp.status == 200
            detail_body = await detail_resp.json()
            assert set(detail_body) == {"ok", "alias", "host"}
            assert detail_body["ok"] is True
            assert detail_body["alias"] == "prod"
            assert detail_body["host"] == {
                "host": "10.0.0.1",
                "port": 2222,
                "user": "deploy",
                "auth": "password",
                "sudo": True,
                "idle_timeout_sec": 1200,
                "allowed_chat_ids": [1],
                "roles": ["web"],
                "description": "Production",
                "remote_project_root": "/srv/app",
                "has_password": True,
                "has_sudo_password": True,
                "has_key": False,
            }

            update_resp = await client.post(
                f"/api/ssh/hosts/update{qs}",
                headers=headers,
                json={
                    "alias": "prod",
                    "host": "10.0.0.2",
                    "port": 2200,
                    "user": "release",
                    "auth": "key",
                    "roles": ["app"],
                    "description": "Updated",
                    "remote_project_root": "/srv/app-v2",
                },
            )
            assert update_resp.status == 200
            assert await update_resp.json() == {"ok": True, "alias": "prod"}

            test_resp = await client.post(
                f"/api/ssh/test-connection{qs}",
                headers=headers,
                json={"alias": "prod"},
            )
            assert test_resp.status == 200
            assert await test_resp.json() == {
                "ok": True,
                "message": "connection ok",
                "server_info": "Linux test-host",
            }
            assert app.ssh_service.test_connection_calls == [(str(workdir), "prod")]

            keygen_resp = await client.post(
                f"/api/ssh/keygen{qs}",
                headers=headers,
                json={"alias": "prod"},
            )
            assert keygen_resp.status == 200
            keygen_body = await keygen_resp.json()
            assert keygen_body == {
                "ok": True,
                "public_key": "ssh-ed25519 AAAAprod",
                "key_path": f"{workdir}/.cli-proxy/ssh/prod",
            }
            assert app.ssh_service.generate_key_calls == [(str(workdir), "prod")]

            secret_resp = await client.post(
                f"/api/ssh/secret{qs}",
                headers=headers,
                json={"key": "SSH_ROUTE_SECRET", "value": "secret-value"},
            )
            assert secret_resp.status == 200
            assert await secret_resp.json() == {"ok": True, "key": "SSH_ROUTE_SECRET"}
            assert load_ssh_secrets(str(workdir))["SSH_ROUTE_SECRET"] == "secret-value"

            delete_resp = await client.post(
                f"/api/ssh/hosts/delete{qs}",
                headers=headers,
                json={"alias": "prod"},
            )
            assert delete_resp.status == 200
            assert await delete_resp.json() == {"ok": True, "alias": "prod"}

            final_list_resp = await client.get(f"/api/ssh/hosts{qs}", headers=headers)
            assert final_list_resp.status == 200
            assert await final_list_resp.json() == {"ok": True, "hosts": {}}
        finally:
            await client.close()
            await server.close()
            app.shutdown_html_process_pool()

    asyncio.run(_run())


def test_files_tree_requires_explicit_session_uid(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path, token="t")
        app = BotApp(cfg)

        web_app = web.Application()
        _register_routes(web_app, app)

        server = TestServer(web_app)
        await server.start_server()
        client = TestClient(server)
        await client.start_server()
        try:
            headers = {"X-Telegram-Init-Data": _build_init_data("t", 1)}
            resp = await client.get("/api/files/tree?path=.", headers=headers)
            assert resp.status == 400
            body = await resp.json()
            assert "session_uid is required" in body.get("error", "")
        finally:
            await client.close()
            await server.close()

    asyncio.run(_run())


def test_files_tree_uses_explicit_session_uid_without_active_session_marker(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path, token="t")
        app = BotApp(cfg)
        restored = app.manager.create(1, "dummy", str(tmp_path))
        session_uid = session_runtime_uid(restored)

        web_app = web.Application()
        _register_routes(web_app, app)

        server = TestServer(web_app)
        await server.start_server()
        client = TestClient(server)
        await client.start_server()
        try:
            headers = {"X-Telegram-Init-Data": _build_init_data("t", 1)}
            resp = await client.get(f"/api/files/tree?path=.&session_uid={session_uid}", headers=headers)
            assert resp.status == 200
        finally:
            await client.close()
            await server.close()

    asyncio.run(_run())


def test_miniapp_files_crud_happy_path(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path, token="t")
        app = BotApp(cfg)
        session = app.manager.create(1, "dummy", str(tmp_path))
        session_uid = session_runtime_uid(session)

        web_app = web.Application()
        _register_routes(web_app, app)

        server = TestServer(web_app)
        await server.start_server()
        client = TestClient(server)
        await client.start_server()
        try:
            headers = {"X-Telegram-Init-Data": _build_init_data("t", 1)}

            resp = await client.post(
                "/api/files/create",
                headers=headers,
                json={"session_uid": session_uid, "path": "notes.txt", "kind": "file"},
            )
            assert resp.status == 200
            assert (await resp.json())["ok"] is True

            resp = await client.post(
                "/api/files/write",
                headers=headers,
                json={"session_uid": session_uid, "path": "notes.txt", "content": "hello miniapp"},
            )
            assert resp.status == 200
            body = await resp.json()
            assert body["ok"] is True
            revision = body["revision"]
            assert isinstance(revision, str) and revision

            resp = await client.get(f"/api/files/read?path=notes.txt&session_uid={session_uid}", headers=headers)
            assert resp.status == 200
            body = await resp.json()
            assert body["content"] == "hello miniapp"
            assert body["revision"] == revision

            resp = await client.get(f"/api/files/meta?path=notes.txt&session_uid={session_uid}", headers=headers)
            assert resp.status == 200
            body = await resp.json()
            assert body["exists"] is True
            assert body["is_dir"] is False

            resp = await client.get(f"/api/files/tree?path=.&session_uid={session_uid}", headers=headers)
            assert resp.status == 200
            items = (await resp.json())["items"]
            assert any(item.get("path") == "notes.txt" for item in items)

            resp = await client.post(
                "/api/files/delete",
                headers=headers,
                json={"session_uid": session_uid, "path": "notes.txt"},
            )
            assert resp.status == 200
            assert (await resp.json())["ok"] is True
        finally:
            await client.close()
            await server.close()

    asyncio.run(_run())


def test_miniapp_files_write_local_revision_mismatch_returns_409_payload(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path, token="t")
        app = BotApp(cfg)
        workdir = tmp_path / "local-conflict"
        workdir.mkdir()
        session = app.manager.create(1, "dummy", str(workdir))
        session_uid = session_runtime_uid(session)
        file_path = workdir / "notes.txt"
        file_path.write_text("version one", encoding="utf-8")

        web_app = web.Application()
        _register_routes(web_app, app)

        server = TestServer(web_app)
        await server.start_server()
        client = TestClient(server)
        await client.start_server()
        try:
            headers = {"X-Telegram-Init-Data": _build_init_data("t", 1)}

            read_resp = await client.get(
                f"/api/files/read?path=notes.txt&session_uid={session_uid}",
                headers=headers,
            )
            assert read_resp.status == 200
            revision = str((await read_resp.json())["revision"])

            file_path.write_text("external update", encoding="utf-8")

            with patch("miniapp.routes.logger.info") as info_mock:
                resp = await client.post(
                    "/api/files/write",
                    headers=headers,
                    json={
                        "session_uid": session_uid,
                        "path": "notes.txt",
                        "content": "user update",
                        "expected_revision": revision,
                    },
                )

            assert resp.status == 409
            body = await resp.json()
            assert body == {
                "ok": False,
                "error": "revision mismatch",
                "expected_revision": revision,
                "current_revision": hashlib.sha256(b"external update").hexdigest(),
                "current_content": "external update",
                "diff_unified": body["diff_unified"],
            }
            assert "--- yours" in body["diff_unified"]
            assert "+++ current" in body["diff_unified"]
            calls = [
                call for call in info_mock.call_args_list
                if call.args and call.args[0] == "remote_file_conflict_detected"
            ]
            assert calls
            extra = calls[-1].kwargs["extra"]
            assert extra["action"] == "remote_file_conflict_detected"
            assert extra["path"] == "notes.txt"
            assert extra["expected_revision"] == revision
            assert extra["status"] == "conflict"
        finally:
            await client.close()
            await server.close()

    asyncio.run(_run())


def test_miniapp_files_write_remote_revision_mismatch_returns_409_payload(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path, token="t")
        app = BotApp(cfg)
        workdir = tmp_path / "remote-conflict"
        workdir.mkdir()
        app.ssh_service = _FakeRemoteConflictSSH(
            {"/srv/app/notes.txt": b"remote content v2"}
        )
        session = app.manager.create(1, "dummy", str(workdir))
        session.modes.ssh_remote_enabled = True
        session.modes.remote_control_enabled = True
        session.modes.remote_control_host_alias = "prod"
        session_uid = session_runtime_uid(session)

        save_ssh_config(str(workdir), {
            "prod": SSHHostConfig(
                host="1.1.1.1",
                user="deploy",
                remote_project_root="/srv/app",
            ),
        })

        web_app = web.Application()
        _register_routes(web_app, app)

        server = TestServer(web_app)
        await server.start_server()
        client = TestClient(server)
        await client.start_server()
        try:
            headers = {"X-Telegram-Init-Data": _build_init_data("t", 1)}
            resp = await client.post(
                "/api/files/write",
                headers=headers,
                json={
                    "session_uid": session_uid,
                    "path": "notes.txt",
                    "content": "client update",
                    "expected_revision": "stale_revision",
                },
            )

            assert resp.status == 409
            body = await resp.json()
            assert body["ok"] is False
            assert body["error"] == "revision mismatch"
            assert body["expected_revision"] == "stale_revision"
            assert body["current_revision"] == hashlib.sha256(b"remote content v2").hexdigest()
            assert body["current_content"] == "remote content v2"
            assert "--- yours" in body["diff_unified"]
            assert "+++ current" in body["diff_unified"]
        finally:
            await client.close()
            await server.close()

    asyncio.run(_run())


def test_miniapp_files_force_save_logs_audit_event(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path, token="t")
        app = BotApp(cfg)
        workdir = tmp_path / "force-save"
        workdir.mkdir()
        session = app.manager.create(1, "dummy", str(workdir))
        session_uid = session_runtime_uid(session)
        file_path = workdir / "notes.txt"
        file_path.write_text("before force", encoding="utf-8")
        old_revision = hashlib.sha256(b"before force").hexdigest()

        web_app = web.Application()
        _register_routes(web_app, app)

        server = TestServer(web_app)
        await server.start_server()
        client = TestClient(server)
        await client.start_server()
        try:
            headers = {"X-Telegram-Init-Data": _build_init_data("t", 1)}

            with patch("miniapp.routes.logger.info") as info_mock:
                resp = await client.post(
                    "/api/files/write",
                    headers=headers,
                    json={
                        "session_uid": session_uid,
                        "path": "notes.txt",
                        "content": "after force",
                        "expected_revision": "stale-revision",
                        "force": True,
                    },
                )

            assert resp.status == 200
            body = await resp.json()
            assert body["ok"] is True
            assert body["forced"] is True
            assert body["old_revision"] == old_revision
            assert file_path.read_text(encoding="utf-8") == "after force"

            calls = [
                call for call in info_mock.call_args_list
                if call.args and call.args[0] == "remote_file_force_saved"
            ]
            assert calls
            extra = calls[-1].kwargs["extra"]
            assert extra["action"] == "remote_file_force_saved"
            assert extra["session_uid"] == session_uid
            assert extra["surface"] == "miniapp"
            assert extra["path"] == "notes.txt"
            assert extra["old_revision"] == old_revision
            assert extra["new_revision"] == body["revision"]
            assert extra["status"] == "ok"
        finally:
            await client.close()
            await server.close()

    asyncio.run(_run())


def test_miniapp_files_conflict_returns_diff_unified(tmp_path) -> None:
    """Test that revision mismatch returns 409 with diff_unified for Conflict UI."""
    async def _run() -> None:
        cfg = _build_config(tmp_path, token="t")
        app = BotApp(cfg)
        workdir = tmp_path / "conflict-test"
        workdir.mkdir()
        session = app.manager.create(1, "dummy", str(workdir))
        session_uid = session_runtime_uid(session)

        # Create initial file
        test_file = workdir / "test.txt"
        test_file.write_text("original content\nline 2\nline 3\n", encoding="utf-8")

        web_app = web.Application()
        _register_routes(web_app, app)

        server = TestServer(web_app)
        await server.start_server()
        client = TestClient(server)
        await client.start_server()
        try:
            headers = {"X-Telegram-Init-Data": _build_init_data("t", 1)}

            # Get current revision (GET request with query params)
            read_resp = await client.get(
                f"/api/files/read?session_uid={session_uid}&path=test.txt",
                headers=headers,
            )
            assert read_resp.status == 200
            read_body = await read_resp.json()
            old_revision = read_body["revision"]

            # Modify file externally
            test_file.write_text("modified externally\nline 2\nline 3\n", encoding="utf-8")

            # Try to save with stale revision - should trigger conflict (409)
            conflict_resp = await client.post(
                "/api/files/write",
                headers=headers,
                json={
                    "session_uid": session_uid,
                    "path": "test.txt",
                    "content": "my changes\nline 2\nline 3\n",
                    "expected_revision": old_revision,
                },
            )
            assert conflict_resp.status == 409
            conflict_body = await conflict_resp.json()
            # Backend returns: diff_unified, current_revision, expected_revision
            assert "diff_unified" in conflict_body
            assert "---" in conflict_body["diff_unified"]
            assert "+++" in conflict_body["diff_unified"]
            assert "current_revision" in conflict_body
            assert "expected_revision" in conflict_body

        finally:
            await client.close()
            await server.close()

    asyncio.run(_run())


def test_miniapp_files_force_save_flow_with_conflict(tmp_path) -> None:
    """Test Force Save flow: conflict detected, then force save succeeds."""
    async def _run() -> None:
        cfg = _build_config(tmp_path, token="t")
        app = BotApp(cfg)
        workdir = tmp_path / "force-save-flow"
        workdir.mkdir()
        session = app.manager.create(1, "dummy", str(workdir))
        session_uid = session_runtime_uid(session)

        # Create initial file
        test_file = workdir / "force.txt"
        test_file.write_text("original\n", encoding="utf-8")

        web_app = web.Application()
        _register_routes(web_app, app)

        server = TestServer(web_app)
        await server.start_server()
        client = TestClient(server)
        await client.start_server()
        try:
            headers = {"X-Telegram-Init-Data": _build_init_data("t", 1)}

            # Get current revision (GET request with query params)
            read_resp = await client.get(
                f"/api/files/read?session_uid={session_uid}&path=force.txt",
                headers=headers,
            )
            assert read_resp.status == 200
            read_body = await read_resp.json()
            old_revision = read_body["revision"]

            # Modify file externally
            test_file.write_text("modified by other\n", encoding="utf-8")

            # First try without force - should get conflict
            conflict_resp = await client.post(
                "/api/files/write",
                headers=headers,
                json={
                    "session_uid": session_uid,
                    "path": "force.txt",
                    "content": "my changes\n",
                    "expected_revision": old_revision,
                },
            )
            assert conflict_resp.status == 409

            # Now force save - should succeed
            force_resp = await client.post(
                "/api/files/write",
                headers=headers,
                json={
                    "session_uid": session_uid,
                    "path": "force.txt",
                    "content": "forced content\n",
                    "expected_revision": old_revision,
                    "force": True,
                },
            )
            assert force_resp.status == 200
            force_body = await force_resp.json()
            assert force_body["ok"] is True
            assert force_body["forced"] is True
            assert "old_revision" in force_body

            # Verify file content changed
            assert test_file.read_text(encoding="utf-8") == "forced content\n"

        finally:
            await client.close()
            await server.close()

    asyncio.run(_run())


def test_miniapp_files_write_via_shared_ingress_accepts_payload_over_default_aiohttp_limit(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path, token="t")
        app = BotApp(cfg)
        session = app.manager.create(1, "dummy", str(tmp_path))
        session_uid = session_runtime_uid(session)
        (tmp_path / "large.txt").write_text("", encoding="utf-8")

        app.shared_http_ingress = SharedHttpIngress(host="127.0.0.1", port=0)
        await app.webhook_ingress_service.start()
        miniapp_server = MiniAppServer(app)
        await miniapp_server.start()
        await app.shared_http_ingress.start()
        port = app.shared_http_ingress.bound_port

        try:
            headers = {"X-Telegram-Init-Data": _build_init_data("t", 1)}
            content = "A" * ((1024 * 1024) + 128)

            async with ClientSession() as client:
                resp = await client.post(
                    f"http://127.0.0.1:{port}/cli-proxy/api/files/write",
                    headers=headers,
                    json={"session_uid": session_uid, "path": "large.txt", "content": content},
                )
                assert resp.status == 200
                body = await resp.json()
                assert body["ok"] is True
                assert isinstance(body.get("revision"), str) and body["revision"]

            assert (tmp_path / "large.txt").read_text(encoding="utf-8") == content
        finally:
            await app.webhook_ingress_service.stop()
            await miniapp_server.stop()
            await app.shared_http_ingress.stop()

    asyncio.run(_run())


def test_miniapp_files_download_happy_path_via_ticket(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path, token="t")
        app = BotApp(cfg)
        session = app.manager.create(1, "dummy", str(tmp_path))
        session_uid = session_runtime_uid(session)
        (tmp_path / "notes.txt").write_text("hello download", encoding="utf-8")

        web_app = web.Application()
        _register_routes(web_app, app)

        server = TestServer(web_app)
        await server.start_server()
        client = TestClient(server)
        await client.start_server()
        try:
            headers = {"X-Telegram-Init-Data": _build_init_data("t", 1)}

            ticket_resp = await client.get("/api/files/ws_ticket", headers=headers)
            assert ticket_resp.status == 200
            ticket = str((await ticket_resp.json()).get("ticket") or "")
            assert ticket

            resp = await client.get(
                f"/api/files/download?ticket={quote(ticket)}&session_uid={quote(session_uid)}&path=notes.txt"
            )
            assert resp.status == 200
            assert resp.headers.get("Content-Disposition", "").startswith("attachment; filename=\"notes.txt\"")
            assert resp.headers.get("Cache-Control") == "no-store"
            assert resp.headers.get("X-Content-Type-Options") == "nosniff"
            assert resp.headers.get("Content-Type", "").startswith("text/plain")
            assert await resp.text() == "hello download"
        finally:
            await client.close()
            await server.close()

    asyncio.run(_run())


def test_miniapp_files_download_binary_non_utf8_returns_raw_bytes(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path, token="t")
        app = BotApp(cfg)
        session = app.manager.create(1, "dummy", str(tmp_path))
        session_uid = session_runtime_uid(session)
        # Записываем файл с байтами, не декодируемыми как UTF-8
        bin_content = b"caf\xe9"
        (tmp_path / "data.bin").write_bytes(bin_content)

        web_app = web.Application()
        _register_routes(web_app, app)

        server = TestServer(web_app)
        await server.start_server()
        client = TestClient(server)
        await client.start_server()
        try:
            headers = {"X-Telegram-Init-Data": _build_init_data("t", 1)}

            ticket_resp = await client.get("/api/files/ws_ticket", headers=headers)
            assert ticket_resp.status == 200
            ticket = str((await ticket_resp.json()).get("ticket") or "")
            assert ticket

            resp = await client.get(
                f"/api/files/download?ticket={quote(ticket)}&session_uid={quote(session_uid)}&path=data.bin"
            )
            assert resp.status == 200
            assert resp.headers.get("Content-Disposition", "").startswith("attachment;")
            assert await resp.read() == bin_content
        finally:
            await client.close()
            await server.close()

    asyncio.run(_run())


def test_miniapp_files_search_uses_remote_execution_target_when_remote_control_on(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path, token="t")
        app = BotApp(cfg)
        workdir = tmp_path / "remote-search"
        workdir.mkdir()
        app.ssh_service = _FakeRemoteShellSSH()

        session = app.manager.create(1, "dummy", str(workdir))
        session.modes.ssh_remote_enabled = True
        session.modes.remote_control_enabled = True
        session.modes.remote_control_host_alias = "prod"
        session_uid = session_runtime_uid(session)

        save_ssh_config(str(workdir), {
            "prod": SSHHostConfig(
                host="1.1.1.1",
                user="deploy",
                remote_project_root="/srv/app",
            ),
        })

        web_app = web.Application()
        _register_routes(web_app, app)

        server = TestServer(web_app)
        await server.start_server()
        client = TestClient(server)
        await client.start_server()
        try:
            headers = {"X-Telegram-Init-Data": _build_init_data("t", 1)}
            resp = await client.get(
                f"/api/files/search?session_uid={session_uid}&pattern=needle&path=src",
                headers=headers,
            )
            assert resp.status == 200
            body = await resp.json()
            assert body["ok"] is True
            assert body["execution_target"] == "remote"
            assert body["truncated"] is False
            assert body["matches"] == [
                {"file": "src/main.py", "line": 12, "text": "needle found remotely"},
            ]

            assert app.ssh_service.calls == [
                (
                    str(workdir),
                    "prod",
                    app.ssh_service.calls[0][2],
                    30,
                ),
            ]
            assert "needle" in app.ssh_service.calls[0][2]
            assert "'src'" in app.ssh_service.calls[0][2]
        finally:
            await client.close()
            await server.close()

    asyncio.run(_run())


def test_miniapp_files_search_streams_local_results_with_global_cap(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path, token="t")
        app = BotApp(cfg)
        workdir = tmp_path / "local-search"
        workdir.mkdir()
        (workdir / "a.txt").write_text("needle 1\nneedle 2\nneedle 3\n", encoding="utf-8")
        session = app.manager.create(1, "dummy", str(workdir))
        session_uid = session_runtime_uid(session)

        web_app = web.Application()
        _register_routes(web_app, app)

        server = TestServer(web_app)
        await server.start_server()
        client = TestClient(server)
        await client.start_server()
        try:
            headers = {"X-Telegram-Init-Data": _build_init_data("t", 1)}
            resp = await client.get(
                f"/api/files/search?session_uid={session_uid}&pattern=needle&path=.&max_results=2",
                headers=headers,
            )
            assert resp.status == 200
            body = await resp.json()
            assert body["execution_target"] == "local"
            assert body["truncated"] is True
            assert len(body["matches"]) == 2
        finally:
            await client.close()
            await server.close()

    asyncio.run(_run())


def test_miniapp_remote_files_recheck_host_acl_for_existing_session(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path, token="t")
        cfg.telegram.whitelist_chat_ids = [2]
        cfg.telegram.admlist_chat_ids = []
        app = BotApp(cfg)
        workdir = tmp_path / "remote-acl"
        workdir.mkdir()
        cfg.telegram.user_workdirs = {2: [str(workdir)]}
        app.ssh_service = _FakeRemoteShellSSH(files={"/srv/app/secret.txt": b"secret"})

        session = app.manager.create(2, "dummy", str(workdir))
        session.modes.ssh_remote_enabled = True
        session.modes.remote_control_enabled = True
        session.modes.remote_control_host_alias = "prod"
        session_uid = session_runtime_uid(session)

        save_ssh_config(str(workdir), {
            "prod": SSHHostConfig(
                host="1.1.1.1",
                user="deploy",
                allowed_chat_ids=[999],
                remote_project_root="/srv/app",
            ),
        })

        web_app = web.Application()
        _register_routes(web_app, app)

        server = TestServer(web_app)
        await server.start_server()
        client = TestClient(server)
        await client.start_server()
        try:
            headers = {"X-Telegram-Init-Data": _build_init_data("t", 2)}
            read_resp = await client.get(
                f"/api/files/read?session_uid={session_uid}&path=secret.txt",
                headers=headers,
            )
            assert read_resp.status == 403

            search_resp = await client.get(
                f"/api/files/search?session_uid={session_uid}&pattern=secret&path=.",
                headers=headers,
            )
            assert search_resp.status == 403
            assert app.ssh_service.calls == []
        finally:
            await client.close()
            await server.close()

    asyncio.run(_run())


def test_miniapp_remote_non_git_target_supports_files_and_search(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path, token="t")
        app = BotApp(cfg)
        workdir = tmp_path / "remote-non-git"
        workdir.mkdir()
        app.ssh_service = _FakeRemoteShellSSH(
            is_git=False,
            search_output="docs/readme.txt:3:needle on plain target\n",
            files={"/srv/plain/readme.txt": b"plain target content"},
        )

        session = app.manager.create(1, "dummy", str(workdir))
        session.modes.ssh_remote_enabled = True
        session.modes.remote_control_enabled = True
        session.modes.remote_control_host_alias = "plain"
        session_uid = session_runtime_uid(session)

        save_ssh_config(str(workdir), {
            "plain": SSHHostConfig(
                host="1.1.1.1",
                user="deploy",
                remote_project_root="/srv/plain",
            ),
        })

        web_app = web.Application()
        _register_routes(web_app, app)

        server = TestServer(web_app)
        await server.start_server()
        client = TestClient(server)
        await client.start_server()
        try:
            headers = {"X-Telegram-Init-Data": _build_init_data("t", 1)}

            tree_resp = await client.get(
                f"/api/files/tree?session_uid={session_uid}&path=.",
                headers=headers,
            )
            assert tree_resp.status == 200
            tree_body = await tree_resp.json()
            assert any(item.get("path") == "readme.txt" for item in tree_body["items"])

            read_resp = await client.get(
                f"/api/files/read?session_uid={session_uid}&path=readme.txt",
                headers=headers,
            )
            assert read_resp.status == 200
            read_body = await read_resp.json()
            assert read_body["content"] == "plain target content"
            assert read_body["revision"] == hashlib.sha256(b"plain target content").hexdigest()

            search_resp = await client.get(
                f"/api/files/search?session_uid={session_uid}&pattern=needle&path=docs",
                headers=headers,
            )
            assert search_resp.status == 200
            search_body = await search_resp.json()
            assert search_body["ok"] is True
            assert search_body["execution_target"] == "remote"
            assert search_body["matches"] == [
                {"file": "docs/readme.txt", "line": 3, "text": "needle on plain target"},
            ]

            assert all(call[1] == "plain" for call in app.ssh_service.calls)
            assert not any("git rev-parse" in call[2] for call in app.ssh_service.calls)
        finally:
            await client.close()
            await server.close()

    asyncio.run(_run())


def test_miniapp_config_endpoints_happy_path(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path, token="t")
        app = BotApp(cfg)
        app.reload_runtime_config = AsyncMock(
            return_value={
                "status": "success_with_warnings",
                "applied": [],
                "restart_required": ["defaults.run_metrics_enabled"],
                "warnings": ["Some changes require process restart."],
            }
        )

        web_app = web.Application()
        _register_routes(web_app, app)

        server = TestServer(web_app)
        await server.start_server()
        client = TestClient(server)
        await client.start_server()
        try:
            headers = {"X-Telegram-Init-Data": _build_init_data("t", 1)}

            schema_resp = await client.get("/api/config/schema", headers=headers)
            assert schema_resp.status == 200
            schema_body = await schema_resp.json()
            assert "sections" in schema_body
            default_backend_meta = schema_body["sections"]["defaults"]["fields"]["default_execution_backend"]
            assert default_backend_meta["type"] == "enum[headless,tmux]"
            assert default_backend_meta["reloadable"] is True

            view_resp = await client.get("/api/config/view", headers=headers)
            assert view_resp.status == 200
            view_body = await view_resp.json()
            assert "revision" in view_body
            assert "config" in view_body

            draft = dict(view_body["config"])
            draft.setdefault("defaults", {})
            draft["defaults"]["idle_timeout_sec"] = int(draft["defaults"].get("idle_timeout_sec", 0)) + 1

            diff_resp = await client.post("/api/config/diff", headers=headers, json={"draft": draft})
            assert diff_resp.status == 200
            diff_body = await diff_resp.json()
            assert isinstance(diff_body.get("changed"), list)
            assert any(item.get("field") == "defaults.idle_timeout_sec" for item in diff_body["changed"])

            validate_resp = await client.post("/api/config/validate", headers=headers, json={"draft": draft})
            assert validate_resp.status == 200
            validate_body = await validate_resp.json()
            assert validate_body["ok"] is True

            save_resp = await client.post(
                "/api/config/save",
                headers=headers,
                json={"draft": draft, "expected_revision": view_body["revision"]},
            )
            assert save_resp.status == 200
            save_body = await save_resp.json()
            assert save_body["ok"] is True
            assert save_body["reload"] == {
                "status": "success_with_warnings",
                "applied": [],
                "restart_required": ["defaults.run_metrics_enabled"],
                "warnings": ["Some changes require process restart."],
            }
            assert isinstance(save_body.get("revision"), str)
        finally:
            await client.close()
            await server.close()

    asyncio.run(_run())


def test_miniapp_config_validate_rejects_typed_invalid_draft(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path, token="t")
        app = BotApp(cfg)

        web_app = web.Application()
        _register_routes(web_app, app)

        server = TestServer(web_app)
        await server.start_server()
        client = TestClient(server)
        await client.start_server()
        try:
            headers = {"X-Telegram-Init-Data": _build_init_data("t", 1)}
            view_resp = await client.get("/api/config/view", headers=headers)
            assert view_resp.status == 200
            view_body = await view_resp.json()

            draft = dict(view_body["config"])
            draft["thread_mode"] = {
                "enabled": True,
                "mode": "group",
                "topics_chat_id": None,
            }

            validate_resp = await client.post("/api/config/validate", headers=headers, json={"draft": draft})
            assert validate_resp.status == 200
            validate_body = await validate_resp.json()
            assert validate_body["ok"] is False
            assert any("thread_mode" in item for item in validate_body["errors"])
            assert any("topics_chat_id" in item for item in validate_body["errors"])
        finally:
            await client.close()
            await server.close()

    asyncio.run(_run())


def test_miniapp_routes_sync_owned_session_projects_for_scheduler_and_launch(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path, token="t")
        app = BotApp(cfg)
        workdir = tmp_path / "miniapp-sync-project"
        workdir.mkdir()

        session = app.manager.create(1, "dummy", str(workdir))
        assert app.project_registry.list_projects(owner_id=1) == []

        published = []

        async def _publish(event) -> None:
            published.append(event)

        app.system_event_bus = SimpleNamespace(publish=_publish)

        web_app = web.Application()
        _register_routes(web_app, app)

        server = TestServer(web_app)
        await server.start_server()
        client = TestClient(server)
        await client.start_server()
        try:
            headers = {"X-Telegram-Init-Data": _build_init_data("t", 1)}

            list_resp = await client.get("/api/v1/scheduler/jobs", headers=headers)
            assert list_resp.status == 200
            list_body = await list_resp.json()
            assert list_body["ok"] is True
            assert len(list_body["projects"]) == 1

            project = list_body["projects"][0]
            record = app.project_registry.require_owner(path=str(workdir), owner_id=1)
            assert project == {
                "slug": record.slug,
                "name": record.name,
                "path": str(workdir.resolve()),
                "enabled": True,
            }

            launch_resp = await client.post(
                "/api/v1/modes/launch",
                headers=headers,
                json={
                    "project_slug": record.slug,
                    "mode_id": "capture",
                    "prompt": "launch from synced project",
                    "session_uid": session_runtime_uid(session),
                },
            )
            assert launch_resp.status == 202
            launch_body = await launch_resp.json()
            assert launch_body["ok"] is True
            assert launch_body["queued"] is True
            assert launch_body["mode_id"] == "capture"
            assert launch_body["project_slug"] == record.slug
            assert launch_body["session_uid"] == session_runtime_uid(session)

            assert len(published) == 1
            event = published[0]
            assert event.user_id == "telegram:1"
            assert event.session_uid == session_runtime_uid(session)
            assert event.project_slug == record.slug
            assert event.command == "capture"
            assert event.payload["prompt"] == "launch from synced project"
            assert event.payload["actor"] == {
                "kind": "miniapp",
                "user_id": 1,
                "actor_id": "telegram:1",
            }
        finally:
            await client.close()
            await server.close()

    asyncio.run(_run())


def test_miniapp_admin_status_endpoint_returns_admin_status_payload(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path, token="t")
        app = BotApp(cfg)
        session = app.manager.create(1, "dummy", str(tmp_path))
        store = AdminStateStore(cfg.defaults.state_path)
        store.upsert_session_state(session.id, chat_id=1, enabled=True)
        session.admin_runtime_status = {
            "pipeline_status": "running",
            "analyzer_status": "completed",
            "analyzer_message": "Первичный анализ завершен",
            "executor_status": "running",
            "executor_message": "Команда выполняется",
            "status_updated_at": 1_700_000_100.0,
        }

        web_app = web.Application()
        _register_routes(web_app, app)

        server = TestServer(web_app)
        await server.start_server()
        client = TestClient(server)
        await client.start_server()
        try:
            headers = {"X-Telegram-Init-Data": _build_init_data("t", 1)}
            resp = await client.get(
                f"/api/v1/admin/status?session_uid={session_runtime_uid(session)}",
                headers=headers,
            )
            assert resp.status == 200

            body = await resp.json()
            validate_admin_payload(body, AdminStatusPayloadSchema, contract="miniapp_admin_status_route_test")
            assert body["session_uid"] == session_runtime_uid(session)
            assert body["session_id"] == session.id
            assert body["active"] is True
            assert body["pipeline_status"] == "running"
            assert body["analyzer_status"] == "completed"
            assert body["executor_status"] == "running"
            assert body["analyzer_message"] == "Первичный анализ завершен"
            assert body["executor_message"] == "Команда выполняется"
        finally:
            await client.close()
            await server.close()

    asyncio.run(_run())


def test_miniapp_admin_action_endpoint_enables_and_disables_selected_session(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path, token="t")
        app = BotApp(cfg)
        workdir = tmp_path / "session"
        workdir.mkdir()
        session = app.manager.create(1, "dummy", str(workdir))

        web_app = web.Application()
        _register_routes(web_app, app)

        server = TestServer(web_app)
        await server.start_server()
        client = TestClient(server)
        await client.start_server()
        try:
            headers = {"X-Telegram-Init-Data": _build_init_data("t", 1)}

            enable_resp = await client.post(
                "/api/v1/admin/action",
                headers=headers,
                json={"action": "enable", "session_uid": session_runtime_uid(session)},
            )
            assert enable_resp.status == 200
            enable_body = await enable_resp.json()
            assert enable_body["ok"] is True
            validate_admin_payload(enable_body["status"], AdminStatusPayloadSchema, contract="miniapp_admin_action_enable")
            assert enable_body["status"]["session_uid"] == session_runtime_uid(session)
            assert enable_body["status"]["active"] is True
            assert enable_body["status"]["pipeline_status"] in {"initializing", "idle", "running", "completed"}

            disable_resp = await client.post(
                "/api/v1/admin/action",
                headers=headers,
                json={"action": "disable", "session_uid": session_runtime_uid(session)},
            )
            assert disable_resp.status == 200
            disable_body = await disable_resp.json()
            assert disable_body["ok"] is True
            validate_admin_payload(disable_body["status"], AdminStatusPayloadSchema, contract="miniapp_admin_action_disable")
            assert disable_body["status"]["session_uid"] == session_runtime_uid(session)
            assert disable_body["status"]["active"] is False
            assert disable_body["status"]["pipeline_status"] == "disabled"
        finally:
            await client.close()
            await server.close()

    asyncio.run(_run())


def test_miniapp_admin_endpoints_accept_thread_session_uid(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path, token="t")
        app = BotApp(cfg)
        workdir = tmp_path / "thread-session"
        workdir.mkdir()
        session = app.manager.create(1, "dummy", str(workdir), message_thread_id=55)
        session_uid = session_runtime_uid(session)

        web_app = web.Application()
        _register_routes(web_app, app)

        server = TestServer(web_app)
        await server.start_server()
        client = TestClient(server)
        await client.start_server()
        try:
            headers = {"X-Telegram-Init-Data": _build_init_data("t", 1)}

            status_resp = await client.get(
                f"/api/v1/admin/status?session_uid={session_uid}",
                headers=headers,
            )
            assert status_resp.status == 200
            status_body = await status_resp.json()
            validate_admin_payload(status_body, AdminStatusPayloadSchema, contract="miniapp_admin_status_thread_scope")
            assert status_body["session_uid"] == session_uid

            action_resp = await client.post(
                "/api/v1/admin/action",
                headers=headers,
                json={"action": "enable", "session_uid": session_uid},
            )
            assert action_resp.status == 200
            action_body = await action_resp.json()
            assert action_body["ok"] is True
            validate_admin_payload(action_body["status"], AdminStatusPayloadSchema, contract="miniapp_admin_action_thread_scope")
            assert action_body["status"]["session_uid"] == session_uid
            assert action_body["status"]["active"] is True
        finally:
            await client.close()
            await server.close()

    asyncio.run(_run())


def test_miniapp_admin_status_and_action_handle_skill_install_approvals(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path, token="t")
        app = BotApp(cfg)
        workdir = tmp_path / "session-skill-approval"
        workdir.mkdir()
        session = app.manager.create(1, "dummy", str(workdir))

        pending = app.mode_skill_runtime.policy_service.register_pending_install(
            session=session,
            mode_id="agent",
            phase="execute",
            task_hash="sha256:miniapp-skill",
            skill_id="playwright-cli-local",
            source="ref:owner-repo-skill",
            acquisition_source="ref:owner-repo-skill",
            ref="owner/repo@playwright-cli-local",
            install_target=app.mode_skill_runtime.policy_service.resolve_install_target(session=session),
            requester={
                "actor_chat_id": "1",
                "session_uid": session_runtime_uid(session),
            },
            origin_payload={
                "candidate": {
                    "skill_id": "playwright-cli-local",
                    "title": "playwright-cli-local",
                    "description": "Skill for browser checks",
                    "source": "ref:owner-repo-skill",
                    "acquisition_source": "ref:owner-repo-skill",
                    "ref": "owner/repo@playwright-cli-local",
                },
                "acquired_skill": {
                    "skill_id": "playwright-cli-local",
                    "title": "playwright-cli-local",
                    "description": "Skill for browser checks",
                    "content": "# playwright-cli-local\n\nUse me.",
                    "source": "ref:owner-repo-skill",
                    "ref": "owner/repo@playwright-cli-local",
                    "tags": ["test"],
                    "metadata": {"created_by": "miniapp-test"},
                },
            },
        )
        assert pending is not None

        web_app = web.Application()
        _register_routes(web_app, app)

        server = TestServer(web_app)
        await server.start_server()
        client = TestClient(server)
        await client.start_server()
        try:
            headers = {"X-Telegram-Init-Data": _build_init_data("t", 1)}

            status_resp = await client.get(
                f"/api/v1/admin/status?session_uid={session_runtime_uid(session)}",
                headers=headers,
            )
            assert status_resp.status == 200
            status_body = await status_resp.json()
            validate_admin_payload(status_body, AdminStatusPayloadSchema, contract="miniapp_admin_skill_pending_status")
            assert status_body["pending_skill_installs"]["count"] == 1
            assert status_body["pending_skill_installs"]["items"][0]["approval_id"] == pending.approval_id

            approve_resp = await client.post(
                "/api/v1/admin/action",
                headers=headers,
                json={
                    "action": "approve_skill_install",
                    "session_uid": session_runtime_uid(session),
                    "approval_id": pending.approval_id,
                },
            )
            assert approve_resp.status == 200
            approve_body = await approve_resp.json()
            assert approve_body["ok"] is True
            assert approve_body["result"]["status"] == "ok"
            assert approve_body["result"]["approval_id"] == pending.approval_id
            assert approve_body["status"]["pending_skill_installs"]["count"] == 0

            installed_manifest = workdir / ".cli-proxy" / "skills" / "playwright-cli-local" / "SKILL.md"
            assert installed_manifest.exists()
        finally:
            await client.close()
            await server.close()

    asyncio.run(_run())


def test_miniapp_admin_status_endpoint_resolves_explicit_session_uid_scope(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path, token="t")
        app = BotApp(cfg)
        workdir_one = tmp_path / "session-chat1"
        workdir_two = tmp_path / "session-chat2"
        workdir_one.mkdir()
        workdir_two.mkdir()
        session_one = app.manager.create(1, "dummy", str(workdir_one))
        session_two = app.manager.create(2, "dummy", str(workdir_two))
        assert session_one.id == session_two.id

        store = AdminStateStore(cfg.defaults.state_path)
        store.upsert_session_state(session_one.id, chat_id=1, enabled=True)
        store.upsert_session_state(session_two.id, chat_id=2, enabled=True)
        session_one.admin_runtime_status = {
            "pipeline_status": "idle",
            "analyzer_status": "idle",
            "analyzer_message": "chat-1",
            "executor_status": "idle",
            "executor_message": "chat-1",
            "status_updated_at": 1_700_000_101.0,
        }
        session_two.admin_runtime_status = {
            "pipeline_status": "running",
            "analyzer_status": "completed",
            "analyzer_message": "chat-2",
            "executor_status": "running",
            "executor_message": "chat-2",
            "status_updated_at": 1_700_000_102.0,
        }

        web_app = web.Application()
        _register_routes(web_app, app)

        server = TestServer(web_app)
        await server.start_server()
        client = TestClient(server)
        await client.start_server()
        try:
            headers = {"X-Telegram-Init-Data": _build_init_data("t", 1)}
            resp = await client.get(
                f"/api/v1/admin/status?session_uid={session_runtime_uid(session_two)}",
                headers=headers,
            )
            assert resp.status == 200

            body = await resp.json()
            assert body["session_uid"] == session_runtime_uid(session_two)
            assert body["session_id"] == session_two.id
            assert body["analyzer_message"] == "chat-2"
            assert body["executor_message"] == "chat-2"
        finally:
            await client.close()
            await server.close()

    asyncio.run(_run())


def test_miniapp_index_contains_logs_autoscroll_toggle(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path, token="t")
        app = BotApp(cfg)

        web_app = web.Application()
        _register_routes(web_app, app)

        server = TestServer(web_app)
        await server.start_server()
        client = TestClient(server)
        await client.start_server()
        try:
            resp = await client.get("/")
            assert resp.status == 200
            body = await resp.text()
            assert 'id="logsAutoScroll"' in body
            assert "Автопрокрутка" in body
        finally:
            await client.close()
            await server.close()

    asyncio.run(_run())


def test_miniapp_index_contains_admin_tab_structure(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path, token="t")
        app = BotApp(cfg)

        web_app = web.Application()
        _register_routes(web_app, app)

        server = TestServer(web_app)
        await server.start_server()
        client = TestClient(server)
        await client.start_server()
        try:
            resp = await client.get("/")
            assert resp.status == 200
            body = await resp.text()
            assert 'data-tab="admin"' in body
            assert 'aria-controls="tab-admin"' in body
            assert ">Админ</button>" in body
            assert '<div id="tab-admin" class="tab"></div>' in body
        finally:
            await client.close()
            await server.close()

    asyncio.run(_run())


def test_miniapp_index_contains_scheduler_tab_structure(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path, token="t")
        app = BotApp(cfg)

        web_app = web.Application()
        _register_routes(web_app, app)

        server = TestServer(web_app)
        await server.start_server()
        client = TestClient(server)
        await client.start_server()
        try:
            resp = await client.get("/")
            assert resp.status == 200
            body = await resp.text()
            assert 'data-tab="scheduler"' in body
            assert 'aria-controls="tab-scheduler"' in body
            assert ">Scheduler</button>" in body
            assert 'id="schedulerProject"' in body
            assert 'id="schedulerSession"' in body
            assert 'id="schedulerSave"' in body
            assert 'id="schedulerRunNow"' in body
        finally:
            await client.close()
            await server.close()

    asyncio.run(_run())
