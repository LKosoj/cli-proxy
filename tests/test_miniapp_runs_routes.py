import asyncio
import hashlib
import hmac
import json
import time
import types
from pathlib import Path
from urllib.parse import quote

import yaml
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from bot import BotApp
from config import AppConfig, DefaultsConfig, MCPConfig, MiniAppConfig, TelegramConfig, ToolConfig
from miniapp.routes import MiniAppRoutes
from miniapp.services.config_service import app_config_to_dict
from session import session_runtime_uid


def _build_init_data(bot_token: str, user_id: int) -> str:
    payload = {
        "auth_date": str(int(time.time())),
        "query_id": "q1",
        "user": json.dumps({"id": user_id, "username": f"user-{user_id}", "first_name": "Mini"}, ensure_ascii=False),
    }
    check = "\n".join(f"{k}={v}" for k, v in sorted(payload.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    sig = hmac.new(secret, check.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"auth_date={payload['auth_date']}&query_id=q1&user={quote(payload['user'])}&hash={sig}"


def _build_config(tmp_path, *, token: str = "123:abc", admins=None, whitelist=None, user_workdirs=None) -> AppConfig:
    if admins is None:
        admins = [999]
    if whitelist is None:
        whitelist = [123]
    if user_workdirs is None:
        user_workdirs = {123: [str(tmp_path)]}
    cfg = AppConfig(
        telegram=TelegramConfig(
            token=token,
            whitelist_chat_ids=list(whitelist),
            admlist_chat_ids=list(admins),
            user_workdirs=dict(user_workdirs),
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
    with open(cfg.path, "w", encoding="utf-8") as handle:
        yaml.safe_dump(app_config_to_dict(cfg), handle, sort_keys=False, allow_unicode=False)
    return cfg


def test_miniapp_runs_routes_return_context_and_enforce_session_visibility(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path)
        app = BotApp(cfg)
        own_session = app.manager.create(123, "dummy", str(tmp_path / "owned"))
        foreign_session = app.manager.create(777, "dummy", str(tmp_path / "foreign"))
        store = app.mode_run_operations.artifact_store

        own_run = store.start_run(
            session=own_session,
            mode_id="agent",
            run_id="run_20260313T101500Z_ownrun01",
            phase="execute",
            source_prompt_hash="sha256:own-run",
        )
        store.save_state(
            own_run,
            {
                "phase": "execute",
                "status": "running",
                "selected_skill_ids": ["playwright-cli"],
                "mode_context": {
                    "cli_work_type": "implementation",
                    "executor_profile": "default",
                },
            },
        )
        store.append_event(
            own_run,
            {
                "event_type": "cli_skill_context_applied",
                "selected_skill_ids": ["playwright-cli"],
            },
        )
        store.append_checkpoint(own_run, {"phase": "execute", "status": "started"})

        terminal_run = store.start_run(
            session=own_session,
            mode_id="agent",
            run_id="run_20260313T101600Z_ownterm1",
            phase="execute",
            source_prompt_hash="sha256:own-run-terminal",
        )
        store.save_state(
            terminal_run,
            {
                "phase": "execute",
                "status": "terminated",
            },
        )

        foreign_run = store.start_run(
            session=foreign_session,
            mode_id="manager",
            run_id="run_20260313T101700Z_foreign1",
            phase="plan",
            source_prompt_hash="sha256:foreign-run",
        )
        store.save_state(foreign_run, {"phase": "plan", "status": "running"})

        web_app = web.Application()
        MiniAppRoutes(app).register(web_app)
        server = TestServer(web_app)
        await server.start_server()
        client = TestClient(server)
        await client.start_server()
        try:
            user_headers = {"X-Telegram-Init-Data": _build_init_data("123:abc", 123)}
            admin_headers = {"X-Telegram-Init-Data": _build_init_data("123:abc", 999)}

            list_resp = await client.get(
                f"/api/runs?session_uid={quote(session_runtime_uid(own_session))}",
                headers=user_headers,
            )
            assert list_resp.status == 200
            list_body = await list_resp.json()
            assert list_body["ok"] is True
            assert list_body["session_uid"] == session_runtime_uid(own_session)
            assert [item["run_id"] for item in list_body["runs"][:2]] == [terminal_run.run_id, own_run.run_id]
            assert list_body["runs"][0]["active"] is False
            assert list_body["runs"][1]["skill_log"] == ["Injected: playwright-cli"]
            assert list_body["runs"][1]["cli_work_type"] == "implementation"

            detail_resp = await client.get(
                f"/api/runs/{own_run.run_id}?session_uid={quote(session_runtime_uid(own_session))}&mode_id=agent",
                headers=user_headers,
            )
            assert detail_resp.status == 200
            detail_body = await detail_resp.json()
            assert detail_body["ok"] is True
            assert detail_body["run"]["state"]["source_prompt_hash"] == "sha256:own-run"
            assert detail_body["run"]["checkpoints"]["items"][0]["status"] == "started"

            denied_resp = await client.get(
                f"/api/runs?session_uid={quote(session_runtime_uid(foreign_session))}",
                headers=user_headers,
            )
            assert denied_resp.status == 404

            admin_resp = await client.get(
                f"/api/runs?session_uid={quote(session_runtime_uid(foreign_session))}",
                headers=admin_headers,
            )
            assert admin_resp.status == 200
            admin_body = await admin_resp.json()
            assert admin_body["runs"][0]["run_id"] == foreign_run.run_id

            # Test terminal_actions_blocked for superseded status
            superseded_run = store.start_run(
                session=own_session,
                mode_id="agent",
                run_id="run_20260313T101800Z_superseded",
                phase="execute",
                source_prompt_hash="sha256:superseded-run",
            )
            store.save_state(
                superseded_run,
                {
                    "phase": "execute",
                    "status": "superseded",
                },
            )
            store.save_recovery(superseded_run, {"can_resume": True})

            list_resp_s = await client.get(
                f"/api/runs?session_uid={quote(session_runtime_uid(own_session))}",
                headers=user_headers,
            )
            assert list_resp_s.status == 200
            list_body_s = await list_resp_s.json()
            runs_by_id = {r["run_id"]: r for r in list_body_s["runs"]}
            s_run_payload = runs_by_id[superseded_run.run_id]
            assert s_run_payload["status"] == "superseded"
            assert s_run_payload["terminal_actions_blocked"] is True
            assert s_run_payload["can_resume"] is False
            assert s_run_payload["can_recover"] is False

            detail_resp_s = await client.get(
                f"/api/runs/{superseded_run.run_id}?session_uid={quote(session_runtime_uid(own_session))}&mode_id=agent",
                headers=user_headers,
            )
            assert detail_resp_s.status == 200
            detail_body_s = await detail_resp_s.json()
            assert detail_body_s["run"]["terminal_actions_blocked"] is True
            assert detail_body_s["run"]["can_resume"] is False
            assert detail_body_s["run"]["can_recover"] is False
        finally:
            await client.close()
            await server.close()

    asyncio.run(_run())


def test_miniapp_run_action_routes_return_recovery_state_payloads(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path)
        app = BotApp(cfg)

        # Stub out the real executor to avoid launching a full pipeline.
        async def _noop_executor(**_kw):
            return {
                "status": "ok",
                "message": "stubbed recovery",
                "executed_operation": "rollback_to_checkpoint",
                "spawned_run_id": "run_20260313T102100Z_spawned",
            }

        app.mode_run_operations.recommended_action_executor = _noop_executor

        session = app.manager.create(123, "dummy", str(tmp_path / "owned"))
        store = app.mode_run_operations.artifact_store

        run = store.start_run(
            session=session,
            mode_id="analyst",
            run_id="run_20260313T102000Z_recover1",
            phase="plan",
            source_prompt_hash="sha256:recover-run",
        )
        store.save_state(
            run,
            {
                "phase": "plan",
                "status": "running",
                "mode_context": {
                    "intent_payload": {
                        "template_id": "default",
                        "user_text": "Проанализируй задачу",
                    },
                    "execution_context": {
                        "dest_kind": "telegram",
                        "chat_id": 123,
                        "user_text_preview": "Проанализируй задачу",
                    },
                },
            },
        )
        Path(run.plan_path).unlink()

        web_app = web.Application()
        MiniAppRoutes(app).register(web_app)
        server = TestServer(web_app)
        await server.start_server()
        client = TestClient(server)
        await client.start_server()
        try:
            headers = {"X-Telegram-Init-Data": _build_init_data("123:abc", 123)}
            admin_headers = {"X-Telegram-Init-Data": _build_init_data("123:abc", 999)}
            doctor_resp = await client.post(
                f"/api/runs/{run.run_id}/doctor",
                headers=headers,
                json={
                    "session_uid": session_runtime_uid(session),
                    "mode_id": "analyst",
                },
            )
            assert doctor_resp.status == 200
            doctor_body = await doctor_resp.json()
            assert doctor_body["ok"] is True
            assert doctor_body["result"]["recommended_action"] == "rollback_to_checkpoint"
            assert any(item["code"] == "missing_plan" for item in doctor_body["result"]["report"]["issues"])
            assert doctor_body["run"]["recovery"]["recommended_action"] == "rollback_to_checkpoint"

            recover_resp = await client.post(
                f"/api/runs/{run.run_id}/recover",
                headers=admin_headers,
                json={
                    "session_uid": session_runtime_uid(session),
                    "mode_id": "analyst",
                },
            )
            assert recover_resp.status == 200
            recover_body = await recover_resp.json()
            assert recover_body["ok"] is True
            assert recover_body["result"]["status"] == "ok"
            assert recover_body["run"]["recovery"]["last_requested_operation"]["operation"] == "recover"
            assert recover_body["run"]["state"]["status"] == "superseded"
            assert recover_body["run"]["recovery"]["recommended_action"] == ""
            assert recover_body["run"]["recovery"]["can_resume"] is False
        finally:
            await client.close()
            await server.close()

    asyncio.run(_run())


def test_miniapp_apply_recommendation_route_uses_shared_backend(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path)
        app = BotApp(cfg)
        session = app.manager.create(123, "dummy", str(tmp_path / "owned"))
        store = app.mode_run_operations.artifact_store

        run = store.start_run(
            session=session,
            mode_id="codebase_mapper",
            run_id="run_20260313T102500Z_mapper1",
            phase="operation",
            source_prompt_hash="sha256:mapper-run",
        )
        store.save_state(
            run,
            {
                "phase": "operation",
                "status": "failed",
                "mode_context": {
                    "operation": "verify",
                    "map_dir": str(Path(run.run_dir) / "artifacts" / "mapper-map"),
                },
            },
        )

        calls = []

        async def _apply_recommendation(*, session, mode_id=None, run_id=None, context=None, dest=None):
            _ = context, dest
            calls.append((session, mode_id, run_id))
            return types.SimpleNamespace(
                operation="apply_recommendation",
                status="ok",
                mode_id=str(mode_id or ""),
                phase="operation",
                message="Validate operation executed.",
                run_id=str(run_id or ""),
                recommended_action="run_validate",
                blocked_by=(),
                report={"status": "needs_recovery"},
            )

        app.mode_run_operations.apply_recommendation_run = _apply_recommendation

        web_app = web.Application()
        MiniAppRoutes(app).register(web_app)
        server = TestServer(web_app)
        await server.start_server()
        client = TestClient(server)
        await client.start_server()
        try:
            headers = {"X-Telegram-Init-Data": _build_init_data("123:abc", 999)}
            action_resp = await client.post(
                f"/api/runs/{run.run_id}/apply_recommendation",
                headers=headers,
                json={
                    "session_uid": session_runtime_uid(session),
                    "mode_id": "codebase_mapper",
                },
            )
            assert action_resp.status == 200
            payload = await action_resp.json()
            assert payload["ok"] is True
            assert payload["action"] == "apply_recommendation"
            assert payload["result"]["recommended_action"] == "run_validate"
            assert payload["result"]["message"] == "Validate operation executed."
            assert calls == [(session, "codebase_mapper", run.run_id)]
        finally:
            await client.close()
            await server.close()

    asyncio.run(_run())


def test_miniapp_write_run_action_denies_non_admin_owner_before_execution(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path)
        app = BotApp(cfg)
        session = app.manager.create(123, "dummy", str(tmp_path / "owned"))
        store = app.mode_run_operations.artifact_store
        run = store.start_run(
            session=session,
            mode_id="agent",
            run_id="run_20260313T103000Z_policydeny",
            phase="execute",
            source_prompt_hash="sha256:policy-deny",
        )
        store.save_state(run, {"phase": "execute", "status": "running", "mode_context": {}})
        calls = []

        class _RunOps:
            artifact_store = store

            async def recover_run(self, **kwargs):
                calls.append(kwargs)
                raise AssertionError("recover_run must be denied before service execution")

        app.mode_run_operations = _RunOps()

        web_app = web.Application()
        MiniAppRoutes(app).register(web_app)
        server = TestServer(web_app)
        await server.start_server()
        client = TestClient(server)
        await client.start_server()
        try:
            resp = await client.post(
                f"/api/runs/{run.run_id}/recover",
                headers={"X-Telegram-Init-Data": _build_init_data("123:abc", 123)},
                json={
                    "session_uid": session_runtime_uid(session),
                    "mode_id": "agent",
                },
            )
            assert resp.status == 403
            payload = await resp.json()
            assert payload["ok"] is False
            assert "admin_required" in payload["error"]
            assert calls == []
        finally:
            await client.close()
            await server.close()

    asyncio.run(_run())


def test_miniapp_promote_skills_route_requires_admin_and_copies_project_local_payload(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path)
        app = BotApp(cfg)
        session = app.manager.create(123, "dummy", str(tmp_path / "owned"))
        project_skill_root = Path(session.workdir) / ".cli-proxy" / "skills" / "playwright-cli"
        project_skill_root.mkdir(parents=True, exist_ok=True)
        (project_skill_root / "SKILL.md").write_text(
            "---\nname: Playwright CLI\ndescription: browser testing skill\n---\n\nbrowser testing skill\n",
            encoding="utf-8",
        )
        (project_skill_root / "notes.txt").write_text("local payload", encoding="utf-8")

        store = app.mode_run_operations.artifact_store
        run = store.start_run(
            session=session,
            mode_id="agent",
            run_id="run_20260313T141500Z_promote1",
            phase="execute",
            source_prompt_hash="sha256:promote-run",
        )
        store.save_state(
            run,
            {
                "phase": "execute",
                "status": "running",
                "selected_skill_ids": ["playwright-cli"],
            },
        )

        web_app = web.Application()
        MiniAppRoutes(app).register(web_app)
        server = TestServer(web_app)
        await server.start_server()
        client = TestClient(server)
        await client.start_server()
        try:
            user_headers = {"X-Telegram-Init-Data": _build_init_data("123:abc", 123)}
            admin_headers = {"X-Telegram-Init-Data": _build_init_data("123:abc", 999)}

            denied_resp = await client.post(
                f"/api/runs/{run.run_id}/promote_skills",
                headers=user_headers,
                json={
                    "session_uid": session_runtime_uid(session),
                    "mode_id": "agent",
                },
            )
            assert denied_resp.status == 403

            promote_resp = await client.post(
                f"/api/runs/{run.run_id}/promote_skills",
                headers=admin_headers,
                json={
                    "session_uid": session_runtime_uid(session),
                    "mode_id": "agent",
                },
            )
            assert promote_resp.status == 200
            promote_body = await promote_resp.json()
            assert promote_body["ok"] is True
            assert promote_body["result"]["status"] == "ok"
            assert promote_body["result"]["promoted_skill_ids"] == ["playwright-cli"]
            promoted_root = Path(cfg.defaults.workdir) / ".cli-proxy" / "skills" / "playwright-cli"
            assert (promoted_root / "SKILL.md").exists()
            assert (promoted_root / "notes.txt").read_text(encoding="utf-8") == "local payload"
        finally:
            await client.close()
            await server.close()

    asyncio.run(_run())
