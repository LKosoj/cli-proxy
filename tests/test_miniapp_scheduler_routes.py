import asyncio
import hashlib
import hmac
import json
import time
from pathlib import Path
from urllib.parse import quote

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
import yaml

from bot import BotApp
from app.services.actor_identity import miniapp_actor_id
from config import (
    AppConfig,
    DefaultsConfig,
    MCPConfig,
    MiniAppConfig,
    SchedulerConfig,
    TelegramConfig,
    ToolConfig,
)
from miniapp.routes import MiniAppRoutes
from miniapp.services.config_service import app_config_to_dict
from session import session_runtime_uid


def _build_init_data(bot_token: str, user_id: int) -> str:
    payload = {
        "auth_date": str(int(time.time())),
        "query_id": "q1",
        "user": json.dumps({"id": user_id, "username": f"user{user_id}", "first_name": f"User{user_id}"}, ensure_ascii=False),
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


def _build_config(tmp_path: Path, *, token: str = "t", user_modes=None) -> AppConfig:
    cfg = AppConfig(
        telegram=TelegramConfig(
            token=token,
            whitelist_chat_ids=[1, 2],
            admlist_chat_ids=[1],
            user_workdirs={2: [str(tmp_path)]},
            user_modes=user_modes or {},
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
        scheduler=SchedulerConfig(
            enabled=True,
            timezone="UTC",
            tick_interval_sec=1,
            max_concurrent_jobs=2,
            misfire_grace_sec=30,
        ),
    )
    with open(cfg.path, "w", encoding="utf-8") as handle:
        yaml.safe_dump(app_config_to_dict(cfg), handle, sort_keys=False, allow_unicode=False)
    return cfg


def _register_project_session(app: BotApp, tmp_path: Path, *, owner_id: int, project_slug: str):
    project_root = tmp_path / project_slug
    workdir = project_root / "workdir"
    workdir.mkdir(parents=True, exist_ok=True)
    project = app.project_registry.register_project(
        path=str(project_root),
        owner_id=owner_id,
        slug=project_slug,
        name=project_slug,
    )
    session = app.manager.create(owner_id, "dummy", str(workdir))
    session.project_root = str(project_root)
    session.name = f"{project_slug}-session"
    return project, session


def test_miniapp_scheduler_routes_crud_and_run_now_for_owned_project(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path, token="t")
        app = BotApp(cfg)
        project, session = _register_project_session(app, tmp_path, owner_id=1, project_slug="alpha")
        telegram_session_uid = session_runtime_uid(session)

        web_app = web.Application()
        MiniAppRoutes(app).register(web_app)

        server = TestServer(web_app)
        await server.start_server()
        client = TestClient(server)
        await client.start_server()
        try:
            headers = {"X-Telegram-Init-Data": _build_init_data("t", 1)}

            resp = await client.get("/api/v1/scheduler/jobs?project_slug=alpha", headers=headers)
            assert resp.status == 200
            body = await resp.json()
            assert [item["slug"] for item in body["projects"]] == ["alpha"]
            assert body["selected_project_slug"] == "alpha"
            assert body["jobs"] == []
            assert [item["telegram_session_uid"] for item in body["notification_targets"]] == [telegram_session_uid]
            assert body["notification_targets"][0]["label"] == f"{session.id} | {session.name} | tg:1"

            create_resp = await client.post(
                "/api/v1/scheduler/jobs",
                headers=headers,
                json={
                    "project_slug": "alpha",
                    "job_name": "Morning digest",
                    "cron": "*/15 * * * *",
                    "target_mode": "manager",
                    "enabled": True,
                    "notification_target": {"telegram_session_uid": telegram_session_uid},
                    "payload": {"project_slug": "alpha", "intent": "digest"},
                },
            )
            assert create_resp.status == 200
            created = (await create_resp.json())["job"]
            assert created["job_name"] == "Morning digest"
            assert created["project_slug"] == "alpha"
            assert created["owner_id"] == miniapp_actor_id(1)
            assert created["notification_target"] == {"telegram_session_uid": telegram_session_uid}
            assert created["last_status"] == "idle"
            assert created["last_error"] == ""
            assert created["run_count"] == 0
            job_id = created["job_id"]

            list_resp = await client.get("/api/v1/scheduler/jobs?project_slug=alpha", headers=headers)
            assert list_resp.status == 200
            jobs = (await list_resp.json())["jobs"]
            assert [item["job_id"] for item in jobs] == [job_id]

            get_resp = await client.get(
                f"/api/v1/scheduler/job?project_slug=alpha&job_id={job_id}",
                headers=headers,
            )
            assert get_resp.status == 200
            fetched = (await get_resp.json())["job"]
            assert fetched["job_id"] == job_id
            assert fetched["last_status"] == "idle"

            update_resp = await client.post(
                "/api/v1/scheduler/jobs/update",
                headers=headers,
                json={
                    "project_slug": "alpha",
                    "job_id": job_id,
                    "job_name": "Updated digest",
                    "cron": "0 * * * *",
                    "target_mode": "agent",
                    "enabled": False,
                    "notification_target": {"telegram_session_uid": telegram_session_uid},
                    "payload": {"project_slug": "alpha", "intent": "updated"},
                },
            )
            assert update_resp.status == 200
            updated = (await update_resp.json())["job"]
            assert updated["job_id"] == job_id
            assert updated["job_name"] == "Updated digest"
            assert updated["cron"] == "0 * * * *"
            assert updated["target_mode"] == "agent"
            assert updated["enabled"] is False
            assert updated["payload"]["intent"] == "updated"
            assert updated["last_status"] == "paused"

            resume_resp = await client.post(
                "/api/v1/scheduler/jobs/resume",
                headers=headers,
                json={"project_slug": "alpha", "job_id": job_id},
            )
            assert resume_resp.status == 200
            resumed = (await resume_resp.json())["job"]
            assert resumed["job_id"] == job_id
            assert resumed["enabled"] is True

            pause_resp = await client.post(
                "/api/v1/scheduler/jobs/pause",
                headers=headers,
                json={"project_slug": "alpha", "job_id": job_id},
            )
            assert pause_resp.status == 200
            paused = (await pause_resp.json())["job"]
            assert paused["job_id"] == job_id
            assert paused["enabled"] is False
            assert paused["last_status"] == "paused"

            resume_again_resp = await client.post(
                "/api/v1/scheduler/jobs/resume",
                headers=headers,
                json={"project_slug": "alpha", "job_id": job_id},
            )
            assert resume_again_resp.status == 200
            resumed_again = (await resume_again_resp.json())["job"]
            assert resumed_again["enabled"] is True

            run_now_resp = await client.post(
                "/api/v1/scheduler/jobs/run_now",
                headers=headers,
                json={"project_slug": "alpha", "job_id": job_id},
            )
            assert run_now_resp.status == 200
            event = (await run_now_resp.json())["event"]
            assert event["job_id"] == job_id
            assert event["status"] == "manual"
            assert event["owner_id"] == miniapp_actor_id(1)
            assert event["notification_target"] == {"telegram_session_uid": telegram_session_uid}

            persisted = app.scheduler_service.get_job(owner_id=1, job_id=job_id)
            assert persisted is not None
            assert persisted.last_fired_at > 0
            assert persisted.last_status == "manual"
            assert persisted.last_error == ""
            assert persisted.run_count == 1

            delete_resp = await client.post(
                "/api/v1/scheduler/jobs/delete",
                headers=headers,
                json={"project_slug": "alpha", "job_id": job_id},
            )
            assert delete_resp.status == 200
            delete_body = await delete_resp.json()
            assert delete_body == {"ok": True, "job_id": job_id}

            final_resp = await client.get("/api/v1/scheduler/jobs?project_slug=alpha", headers=headers)
            assert final_resp.status == 200
            final_body = await final_resp.json()
            assert final_body["jobs"] == []
            assert final_body["selected_project_slug"] == str(project.slug)
        finally:
            await client.close()
            await server.close()

    asyncio.run(_run())


def test_miniapp_scheduler_routes_reject_forbidden_target_mode(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path, token="t", user_modes={2: ["agent"]})
        app = BotApp(cfg)
        _project, session = _register_project_session(app, tmp_path, owner_id=2, project_slug="alpha")
        telegram_session_uid = session_runtime_uid(session)

        web_app = web.Application()
        MiniAppRoutes(app).register(web_app)

        server = TestServer(web_app)
        await server.start_server()
        client = TestClient(server)
        await client.start_server()
        try:
            headers = {"X-Telegram-Init-Data": _build_init_data("t", 2)}

            create_resp = await client.post(
                "/api/v1/scheduler/jobs",
                headers=headers,
                json={
                    "project_slug": "alpha",
                    "job_name": "Forbidden manager",
                    "cron": "*/15 * * * *",
                    "target_mode": "manager",
                    "enabled": True,
                    "notification_target": {"telegram_session_uid": telegram_session_uid},
                    "payload": {"project_slug": "alpha"},
                },
            )
            assert create_resp.status == 403

            allowed_resp = await client.post(
                "/api/v1/scheduler/jobs",
                headers=headers,
                json={
                    "project_slug": "alpha",
                    "job_name": "Allowed agent",
                    "cron": "*/15 * * * *",
                    "target_mode": "agent",
                    "enabled": True,
                    "notification_target": {"telegram_session_uid": telegram_session_uid},
                    "payload": {"project_slug": "alpha"},
                },
            )
            assert allowed_resp.status == 200
            job_id = (await allowed_resp.json())["job"]["job_id"]

            update_resp = await client.post(
                "/api/v1/scheduler/jobs/update",
                headers=headers,
                json={
                    "project_slug": "alpha",
                    "job_id": job_id,
                    "target_mode": "manager",
                    "notification_target": {"telegram_session_uid": telegram_session_uid},
                },
            )
            assert update_resp.status == 403
        finally:
            await client.close()
            await server.close()

    asyncio.run(_run())


def test_miniapp_scheduler_routes_reject_non_owner_requests(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path, token="t")
        app = BotApp(cfg)
        owner_project, owner_session = _register_project_session(app, tmp_path, owner_id=1, project_slug="alpha")
        _register_project_session(app, tmp_path, owner_id=2, project_slug="beta")

        created = app.scheduler_service.create_job(
            owner_id=1,
            cron="*/10 * * * *",
            target_mode="manager",
            notification_target_telegram_session_uid=session_runtime_uid(owner_session),
            payload={"project_slug": owner_project.slug, "intent": "owner-only"},
            job_name="Owner only",
        )

        web_app = web.Application()
        MiniAppRoutes(app).register(web_app)

        server = TestServer(web_app)
        await server.start_server()
        client = TestClient(server)
        await client.start_server()
        try:
            headers = {"X-Telegram-Init-Data": _build_init_data("t", 2)}

            list_resp = await client.get("/api/v1/scheduler/jobs?project_slug=alpha", headers=headers)
            assert list_resp.status == 403

            create_resp = await client.post(
                "/api/v1/scheduler/jobs",
                headers=headers,
                json={
                    "project_slug": "alpha",
                    "job_name": "Intruder",
                    "cron": "*/5 * * * *",
                    "target_mode": "manager",
                    "notification_target": {"telegram_session_uid": "chat:2"},
                    "payload": {"project_slug": "alpha"},
                },
            )
            assert create_resp.status == 403

            update_resp = await client.post(
                "/api/v1/scheduler/jobs/update",
                headers=headers,
                json={"project_slug": "alpha", "job_id": created.job_id},
            )
            assert update_resp.status == 403

            delete_resp = await client.post(
                "/api/v1/scheduler/jobs/delete",
                headers=headers,
                json={"project_slug": "alpha", "job_id": created.job_id},
            )
            assert delete_resp.status == 403

            get_resp = await client.get(
                f"/api/v1/scheduler/job?project_slug=alpha&job_id={created.job_id}",
                headers=headers,
            )
            assert get_resp.status == 403

            pause_resp = await client.post(
                "/api/v1/scheduler/jobs/pause",
                headers=headers,
                json={"project_slug": "alpha", "job_id": created.job_id},
            )
            assert pause_resp.status == 403

            resume_resp = await client.post(
                "/api/v1/scheduler/jobs/resume",
                headers=headers,
                json={"project_slug": "alpha", "job_id": created.job_id},
            )
            assert resume_resp.status == 403

            run_now_resp = await client.post(
                "/api/v1/scheduler/jobs/run_now",
                headers=headers,
                json={"project_slug": "alpha", "job_id": created.job_id},
            )
            assert run_now_resp.status == 403
        finally:
            await client.close()
            await server.close()

    asyncio.run(_run())


def test_miniapp_scheduler_routes_keep_project_scope_isolated_between_requests(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path, token="t")
        app = BotApp(cfg)
        alpha_project, alpha_session = _register_project_session(app, tmp_path, owner_id=1, project_slug="alpha")
        beta_project, beta_session = _register_project_session(app, tmp_path, owner_id=1, project_slug="beta")

        app.scheduler_service.create_job(
            owner_id=1,
            cron="*/5 * * * *",
            target_mode="manager",
            notification_target_telegram_session_uid=session_runtime_uid(alpha_session),
            payload={"project_slug": alpha_project.slug, "intent": "alpha"},
            job_name="Alpha job",
            job_id="job-alpha",
        )
        app.scheduler_service.create_job(
            owner_id=1,
            cron="0 * * * *",
            target_mode="agent",
            notification_target_telegram_session_uid=session_runtime_uid(beta_session),
            payload={"project_slug": beta_project.slug, "intent": "beta"},
            job_name="Beta job",
            job_id="job-beta",
        )

        web_app = web.Application()
        MiniAppRoutes(app).register(web_app)

        server = TestServer(web_app)
        await server.start_server()
        client = TestClient(server)
        await client.start_server()
        try:
            headers = {"X-Telegram-Init-Data": _build_init_data("t", 1)}

            alpha_resp = await client.get("/api/v1/scheduler/jobs?project_slug=alpha", headers=headers)
            assert alpha_resp.status == 200
            alpha_jobs = (await alpha_resp.json())["jobs"]
            assert [item["job_id"] for item in alpha_jobs] == ["job-alpha"]

            beta_resp = await client.get("/api/v1/scheduler/jobs?project_slug=beta", headers=headers)
            assert beta_resp.status == 200
            beta_jobs = (await beta_resp.json())["jobs"]
            assert [item["job_id"] for item in beta_jobs] == ["job-beta"]
        finally:
            await client.close()
            await server.close()

    asyncio.run(_run())


def test_pause_resume_project_scope_validation(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path, token="t")
        app = BotApp(cfg)
        alpha_project, alpha_session = _register_project_session(app, tmp_path, owner_id=1, project_slug="alpha")
        beta_project, beta_session = _register_project_session(app, tmp_path, owner_id=1, project_slug="beta")

        active_job = app.scheduler_service.create_job(
            owner_id=1,
            cron="*/5 * * * *",
            target_mode="manager",
            notification_target_telegram_session_uid=session_runtime_uid(alpha_session),
            payload={"project_slug": alpha_project.slug, "intent": "alpha-active"},
            job_name="Alpha active",
            job_id="job-alpha-active",
        )
        paused_job = app.scheduler_service.create_job(
            owner_id=1,
            cron="0 * * * *",
            target_mode="agent",
            notification_target_telegram_session_uid=session_runtime_uid(beta_session),
            payload={"project_slug": alpha_project.slug, "intent": "alpha-paused"},
            job_name="Alpha paused",
            job_id="job-alpha-paused",
            enabled=False,
        )

        web_app = web.Application()
        MiniAppRoutes(app).register(web_app)

        server = TestServer(web_app)
        await server.start_server()
        client = TestClient(server)
        await client.start_server()
        try:
            headers = {"X-Telegram-Init-Data": _build_init_data("t", 1)}

            pause_for_other_project = await client.post(
                "/api/v1/scheduler/jobs/pause",
                headers=headers,
                json={"project_slug": beta_project.slug, "job_id": active_job.job_id},
            )
            assert pause_for_other_project.status == 403
            pause_error = await pause_for_other_project.json()
            assert pause_error["error"] == "scheduled job does not belong to project"

            persisted_active = app.scheduler_service.get_job(owner_id=1, job_id=active_job.job_id)
            assert persisted_active is not None
            assert persisted_active.enabled is True
            assert persisted_active.last_status == "idle"

            resume_for_other_project = await client.post(
                "/api/v1/scheduler/jobs/resume",
                headers=headers,
                json={"project_slug": beta_project.slug, "job_id": paused_job.job_id},
            )
            assert resume_for_other_project.status == 403
            resume_error = await resume_for_other_project.json()
            assert resume_error["error"] == "scheduled job does not belong to project"

            persisted_paused = app.scheduler_service.get_job(owner_id=1, job_id=paused_job.job_id)
            assert persisted_paused is not None
            assert persisted_paused.enabled is False
            assert persisted_paused.last_status == "paused"

            pause_owned = await client.post(
                "/api/v1/scheduler/jobs/pause",
                headers=headers,
                json={"project_slug": alpha_project.slug, "job_id": active_job.job_id},
            )
            assert pause_owned.status == 200
            paused_body = await pause_owned.json()
            assert paused_body["job"]["enabled"] is False
            assert paused_body["job"]["project_slug"] == alpha_project.slug

            resume_owned = await client.post(
                "/api/v1/scheduler/jobs/resume",
                headers=headers,
                json={"project_slug": alpha_project.slug, "job_id": paused_job.job_id},
            )
            assert resume_owned.status == 200
            resumed_body = await resume_owned.json()
            assert resumed_body["job"]["enabled"] is True
            assert resumed_body["job"]["project_slug"] == alpha_project.slug

            persisted_active_after_pause = app.scheduler_service.get_job(owner_id=1, job_id=active_job.job_id)
            assert persisted_active_after_pause is not None
            assert persisted_active_after_pause.enabled is False
            assert persisted_active_after_pause.last_status == "paused"

            persisted_paused_after_resume = app.scheduler_service.get_job(owner_id=1, job_id=paused_job.job_id)
            assert persisted_paused_after_resume is not None
            assert persisted_paused_after_resume.enabled is True
        finally:
            await client.close()
            await server.close()

    asyncio.run(_run())
