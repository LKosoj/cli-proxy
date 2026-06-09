import asyncio
import hashlib
import hmac
import json
import os
import re
import tempfile
import time
import uuid
from pathlib import Path
from unittest.mock import AsyncMock
from urllib.parse import quote

from aiohttp import web
from aiohttp.test_utils import TestServer
import yaml

from app.services.app_runtime_service import AppRuntimeService
from bot import BotApp
from config import (
    AppConfig,
    DefaultsConfig,
    MCPConfig,
    MiniAppConfig,
    SchedulerConfig,
    TelegramConfig,
    ToolConfig,
    load_config,
)
from miniapp.routes import MiniAppRoutes
from miniapp.services.config_service import app_config_to_dict
from session import session_runtime_uid


_PLAYWRIGHT_RESULT_RE = re.compile(r"### Result\s*(\{.*?\})\s*### Ran Playwright code", re.S)


def _playwright_cli_args() -> list[str]:
    args = ["playwright-cli"]
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        config_path = Path(tempfile.gettempdir()) / "cli-proxy-playwright-cli-root-config.json"
        if not config_path.exists():
            config_path.write_text(
                json.dumps({"browser": {"launchOptions": {"chromiumSandbox": False}}}),
                encoding="utf-8",
            )
        args.extend(["--config", str(config_path)])
    return args


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


def _build_config(tmp_path: Path, *, token: str = "t") -> AppConfig:
    cfg = AppConfig(
        telegram=TelegramConfig(token=token, whitelist_chat_ids=[1, 2], admlist_chat_ids=[1]),
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


def _write_config_file(cfg: AppConfig) -> None:
    Path(cfg.path).write_text(
        yaml.safe_dump(app_config_to_dict(cfg), sort_keys=False, allow_unicode=False),
        encoding="utf-8",
    )


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


async def _run_playwright(session_name: str, *args: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        *_playwright_cli_args(),
        "-s",
        session_name,
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    out_text = stdout.decode("utf-8", errors="replace")
    err_text = stderr.decode("utf-8", errors="replace")
    if proc.returncode != 0:
        raise AssertionError(
            f"playwright-cli {' '.join(args)} failed with code {proc.returncode}\nSTDOUT:\n{out_text}\nSTDERR:\n{err_text}"
        )
    return out_text


def _extract_result_json(stdout: str) -> dict:
    match = _PLAYWRIGHT_RESULT_RE.search(stdout or "")
    if not match:
        raise AssertionError(f"playwright-cli result JSON not found in output:\n{stdout}")
    return json.loads(match.group(1))


async def _playwright_eval_json(session_name: str, script: str) -> dict:
    output = await _run_playwright(session_name, "eval", script)
    return _extract_result_json(output)


async def _playwright_run_code_json(session_name: str, script: str) -> dict:
    output = await _run_playwright(session_name, "run-code", script)
    return _extract_result_json(output)


def _miniapp_boot_script(base_url: str, init_data: str) -> str:
    init_payload = json.dumps(str(init_data))
    page_url = json.dumps(base_url)
    init_script = (
        "await page.addInitScript((initData) => { "
        "const ensureTelegram = (value) => { "
        "  const target = value && typeof value === 'object' ? value : {}; "
        "  const webApp = target.WebApp && typeof target.WebApp === 'object' ? target.WebApp : {}; "
        "  webApp.initData = initData; "
        "  webApp.ready = typeof webApp.ready === 'function' ? webApp.ready : (() => {}); "
        "  webApp.expand = typeof webApp.expand === 'function' ? webApp.expand : (() => {}); "
        "  target.WebApp = webApp; "
        "  return target; "
        "}; "
        "let telegramValue = ensureTelegram(window.Telegram); "
        "Object.defineProperty(window, 'Telegram', { "
        "  configurable: true, "
        "  get() { return telegramValue; }, "
        "  set(value) { telegramValue = ensureTelegram(value); } "
        "}); "
        "window.Telegram = telegramValue; "
        "const originalFetch = window.fetch ? window.fetch.bind(window) : null; "
        "if (originalFetch) { "
        "  window.fetch = (input, init = {}) => { "
        "    const requestInit = init && typeof init === 'object' ? { ...init } : {}; "
        "    const headers = new Headers(requestInit.headers || {}); "
        "    if (!headers.get('X-Telegram-Init-Data')) headers.set('X-Telegram-Init-Data', initData); "
        "    requestInit.headers = headers; "
        "    return originalFetch(input, requestInit); "
        "  }; "
        "} "
        f"}}, {init_payload});"
    )
    return (
        "async page => {"
        f"{init_script}"
        f"await page.goto({page_url}, {{ waitUntil: 'domcontentloaded' }});"
        "return {"
        "  title: await page.title(),"
        "  hasBody: !!(await page.$('body'))"
        "};"
        "}"
    )


def _miniapp_webhooks_ui_toggle_script() -> str:
    return (
        "async page => {"
        "  await page.waitForSelector('.tabs button[data-tab=\"config\"]', { state: 'attached', timeout: 20000 });"
        "  await page.evaluate(() => document.querySelector('.tabs button[data-tab=\"config\"]')?.click());"
        "  await page.waitForSelector('button[data-cfg-section=\"webhooks\"]', { state: 'attached', timeout: 20000 });"
        "  await page.evaluate(() => document.querySelector('button[data-cfg-section=\"webhooks\"]')?.click());"
        "  await page.waitForSelector('#webhooks-enabled', { state: 'attached', timeout: 20000 });"
        "  const before = await page.$eval('#webhooks-enabled', (el) => {"
        "    const hint = el.parentElement && el.parentElement.querySelector('small');"
        "    return { checked: !!el.checked, hint: hint ? String(hint.textContent || '').trim() : '' };"
        "  });"
        "  await page.evaluate(() => document.getElementById('webhooks-enabled')?.click());"
        "  const after = await page.$eval('#webhooks-enabled', (el) => ({ checked: !!el.checked }));"
        "  const configTabActive = await page.$eval('#tab-config', (el) => el.classList.contains('active'));"
        "  return { before, after, configTabActive };"
        "}"
    )


def _miniapp_secret_dom_and_unchanged_save_script(*, model_value: str, real_secrets: list[str]) -> str:
    args_payload = json.dumps(
        {
            "modelValue": str(model_value),
            "realSecrets": list(real_secrets),
        },
        ensure_ascii=False,
    )
    return (
        "async page => {"
        f"  const args = {args_payload};"
        "  await page.evaluate(() => {"
        "    window.__miniappConfirmMessages = [];"
        "    window.confirm = (message) => {"
        "      window.__miniappConfirmMessages.push(String(message));"
        "      return true;"
        "    };"
        "  });"
        "  await page.waitForSelector('.tabs button[data-tab=\"config\"]', { state: 'attached', timeout: 20000 });"
        "  await page.click('.tabs button[data-tab=\"config\"]');"
        "  await page.waitForSelector('button[data-cfg-section=\"defaults\"]', { state: 'attached', timeout: 20000 });"
        "  await page.click('button[data-cfg-section=\"defaults\"]');"
        "  await page.waitForSelector('button[data-defaults-subtab=\"apikeys\"]', { state: 'attached', timeout: 20000 });"
        "  await page.click('button[data-defaults-subtab=\"apikeys\"]');"
        "  await page.waitForSelector('#def-openai-api-key', { state: 'attached', timeout: 20000 });"
        "  const html = await page.content();"
        "  const inputsBefore = await page.$$eval('[data-secret-path]', (items) => items.map((item) => ({"
        "    path: item.getAttribute('data-secret-path'),"
        "    type: item.getAttribute('type'),"
        "    value: item.value"
        "  })));"
        "  await page.fill('#def-openai-model', args.modelValue);"
        "  await page.waitForFunction(() => !document.getElementById('cfgSave').disabled, null, { timeout: 10000 });"
        "  const savePromise = page.waitForResponse((response) =>"
        "    response.url().endsWith('/api/config/save') && response.request().method() === 'POST'"
        "  );"
        "  await page.click('#cfgSave');"
        "  const saveResponse = await savePromise;"
        "  const saveBody = await saveResponse.json();"
        "  await page.waitForSelector('#def-openai-api-key', { state: 'attached', timeout: 20000 });"
        "  const dialogs = await page.evaluate(() => (window.__miniappConfirmMessages || []).map((message) => ({"
        "    type: 'confirm',"
        "    message"
        "  })));"
        "  return {"
        "    inputsBefore,"
        "    realSecretsInDom: args.realSecrets.filter((secret) => html.includes(secret)),"
        "    dialogs,"
        "    saveStatus: saveResponse.status(),"
        "    saveBody"
        "  };"
        "}"
    )


def _miniapp_changed_secret_save_script(*, new_secret: str) -> str:
    args_payload = json.dumps({"newSecret": str(new_secret)}, ensure_ascii=False)
    return (
        "async page => {"
        f"  const args = {args_payload};"
        "  await page.evaluate(() => {"
        "    window.__miniappConfirmMessages = [];"
        "    window.confirm = (message) => {"
        "      window.__miniappConfirmMessages.push(String(message));"
        "      return true;"
        "    };"
        "  });"
        "  await page.waitForSelector('.tabs button[data-tab=\"config\"]', { state: 'attached', timeout: 20000 });"
        "  await page.click('.tabs button[data-tab=\"config\"]');"
        "  await page.waitForSelector('button[data-cfg-section=\"defaults\"]', { state: 'attached', timeout: 20000 });"
        "  await page.click('button[data-cfg-section=\"defaults\"]');"
        "  await page.waitForSelector('button[data-defaults-subtab=\"apikeys\"]', { state: 'attached', timeout: 20000 });"
        "  await page.click('button[data-defaults-subtab=\"apikeys\"]');"
        "  await page.waitForSelector('#def-openai-api-key', { state: 'attached', timeout: 20000 });"
        "  await page.fill('#def-openai-api-key', args.newSecret);"
        "  const filledValue = await page.$eval('#def-openai-api-key', (item) => item.value);"
        "  await page.waitForFunction(() => !document.getElementById('cfgSave').disabled, null, { timeout: 10000 });"
        "  const savePromise = page.waitForResponse((response) =>"
        "    response.url().endsWith('/api/config/save') && response.request().method() === 'POST'"
        "  );"
        "  await page.click('#cfgSave');"
        "  const saveResponse = await savePromise;"
        "  const saveBody = await saveResponse.json();"
        "  const dialogs = await page.evaluate(() => (window.__miniappConfirmMessages || []).map((message) => ({"
        "    type: 'confirm',"
        "    message"
        "  })));"
        "  return {"
        "    filledValue,"
        "    dialogs,"
        "    saveStatus: saveResponse.status(),"
        "    saveBody"
        "  };"
        "}"
    )


def _miniapp_scheduler_fetch_script(path: str, init_data: str, payload: dict[str, str]) -> str:
    args_payload = json.dumps(
        {
            "path": str(path or "").strip(),
            "initData": str(init_data or ""),
            "payload": dict(payload or {}),
        },
        ensure_ascii=False,
    )
    return (
        "async page => {"
        "return await page.evaluate(async ({ path, initData, payload }) => {"
        "  const response = await fetch(path, {"
        "    method: 'POST',"
        "    headers: {"
        "      'Content-Type': 'application/json',"
        "      'X-Telegram-Init-Data': initData"
        "    },"
        "    body: JSON.stringify(payload)"
        "  });"
        "  return { status: response.status, body: await response.json() };"
        f"}}, {args_payload});"
        "}"
    )


def _miniapp_json_fetch_script(
    path: str,
    init_data: str,
    *,
    method: str = "GET",
    payload: dict | None = None,
) -> str:
    args_payload = json.dumps(
        {
            "path": str(path or "").strip(),
            "initData": str(init_data or ""),
            "method": str(method or "GET").upper(),
            "payload": payload,
        },
        ensure_ascii=False,
    )
    return (
        "async page => {"
        "return await page.evaluate(async ({ path, initData, method, payload }) => {"
        "  const isGet = method === 'GET';"
        "  const hasBody = !isGet && payload != null;"
        "  const response = await fetch(path, {"
        "    method,"
        "    headers: {"
        "      'Content-Type': 'application/json',"
        "      'X-Telegram-Init-Data': initData"
        "    },"
        "    body: hasBody ? JSON.stringify(payload) : undefined"
        "  });"
        "  return { status: response.status, body: await response.json() };"
        f"}}, {args_payload});"
        "}"
    )


def _miniapp_raw_json_fetch_script(
    path: str,
    init_data: str,
    *,
    method: str = "POST",
    body: str = "{",
) -> str:
    args_payload = json.dumps(
        {
            "path": str(path or "").strip(),
            "initData": str(init_data or ""),
            "method": str(method or "POST").upper(),
            "body": str(body or ""),
        },
        ensure_ascii=False,
    )
    return (
        "async page => {"
        "return await page.evaluate(async ({ path, initData, method, body }) => {"
        "  const response = await fetch(path, {"
        "    method,"
        "    headers: {"
        "      'Content-Type': 'application/json',"
        "      'X-Telegram-Init-Data': initData"
        "    },"
        "    body"
        "  });"
        "  const raw = await response.text();"
        "  let parsed;"
        "  try {"
        "    parsed = JSON.parse(raw);"
        "  } catch (_error) {"
        "    parsed = { raw };"
        "  }"
        "  return { status: response.status, body: parsed };"
        f"}}, {args_payload});"
        "}"
    )


def _miniapp_status_session_options_script(*, expected_session_uid: str, timeout_ms: int = 10000) -> str:
    args_payload = json.dumps(
        {
            "expectedSessionUid": str(expected_session_uid or "").strip(),
            "timeoutMs": int(timeout_ms),
        },
        ensure_ascii=False,
    )
    return (
        "async page => {"
        "return await page.evaluate(async ({ expectedSessionUid, timeoutMs }) => {"
        "  const startedAt = Date.now();"
        "  while (Date.now() - startedAt < timeoutMs) {"
        "    const select = document.getElementById('statusSession');"
        "    const options = select ? Array.from(select.options || []).map((item) => ({"
        "      value: String(item.value || ''),"
        "      label: String(item.textContent || '').trim()"
        "    })) : [];"
        "    if (options.some((item) => item.value === expectedSessionUid)) {"
        "      return {"
        "        found: true,"
        "        options"
        "      };"
        "    }"
        "    await new Promise((resolve) => window.setTimeout(resolve, 250));"
        "  }"
        "  const select = document.getElementById('statusSession');"
        "  return {"
        "    found: false,"
        "    options: select ? Array.from(select.options || []).map((item) => ({"
        "      value: String(item.value || ''),"
        "      label: String(item.textContent || '').trim()"
        "    })) : []"
        "  };"
        f"}}, {args_payload});"
        "}"
    )


def test_status_session_selector_refreshes_when_new_session_is_created_web(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path, token="t")
        app = BotApp(cfg)
        first_workdir = tmp_path / "first"
        first_workdir.mkdir()
        first_session = app.manager.create(1, "dummy", str(first_workdir))

        web_app = web.Application()
        MiniAppRoutes(app).register(web_app)

        server = TestServer(web_app)
        await server.start_server()
        session_name = f"miniapp-status-sessions-{uuid.uuid4().hex[:8]}"
        try:
            base_url = str(server.make_url("/"))
            init_data = _build_init_data("t", 1)

            await _run_playwright(session_name, "open", "about:blank", "--browser=chrome")
            boot = await _playwright_run_code_json(
                session_name,
                _miniapp_boot_script(base_url, init_data),
            )
            assert boot == {"title": "cli-proxy MiniApp", "hasBody": True}

            first_options = await _playwright_run_code_json(
                session_name,
                _miniapp_status_session_options_script(expected_session_uid=session_runtime_uid(first_session)),
            )
            assert first_options["found"] is True

            second_workdir = tmp_path / "second"
            second_workdir.mkdir()
            second_session = app.manager.create(1, "dummy", str(second_workdir))

            second_options = await _playwright_run_code_json(
                session_name,
                _miniapp_status_session_options_script(expected_session_uid=session_runtime_uid(second_session)),
            )
            assert second_options["found"] is True
            assert [item["value"] for item in second_options["options"]] == [
                "",
                session_runtime_uid(first_session),
                session_runtime_uid(second_session),
            ]
        finally:
            try:
                await _run_playwright(session_name, "close")
            except AssertionError:
                pass
            await server.close()

    asyncio.run(_run())


def test_pause_resume_project_scope_web(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path, token="t")
        app = BotApp(cfg)
        alpha_project, alpha_session = _register_project_session(app, tmp_path, owner_id=1, project_slug="alpha")
        beta_project, _beta_session = _register_project_session(app, tmp_path, owner_id=1, project_slug="beta")

        active_job = app.scheduler_service.create_job(
            owner_id=1,
            cron="*/5 * * * *",
            target_mode="manager",
            notification_target_telegram_session_uid=alpha_session.conversation_scope.session_uid,
            payload={"project_slug": alpha_project.slug, "intent": "alpha-active"},
            job_name="Alpha active",
            job_id="job-alpha-active",
        )
        paused_job = app.scheduler_service.create_job(
            owner_id=1,
            cron="0 * * * *",
            target_mode="agent",
            notification_target_telegram_session_uid=alpha_session.conversation_scope.session_uid,
            payload={"project_slug": alpha_project.slug, "intent": "alpha-paused"},
            job_name="Alpha paused",
            job_id="job-alpha-paused",
            enabled=False,
        )

        web_app = web.Application()
        MiniAppRoutes(app).register(web_app)

        server = TestServer(web_app)
        await server.start_server()
        session_name = f"miniapp-scheduler-scope-{uuid.uuid4().hex[:8]}"
        try:
            base_url = str(server.make_url("/"))
            init_data = _build_init_data("t", 1)

            await _run_playwright(session_name, "open", "about:blank", "--browser=chrome")
            boot = await _playwright_run_code_json(
                session_name,
                _miniapp_boot_script(base_url, init_data),
            )
            assert boot == {"title": "cli-proxy MiniApp", "hasBody": True}

            pause_mismatch = await _playwright_run_code_json(
                session_name,
                _miniapp_scheduler_fetch_script(
                    "/api/v1/scheduler/jobs/pause",
                    init_data,
                    {"project_slug": beta_project.slug, "job_id": active_job.job_id},
                ),
            )
            assert pause_mismatch["status"] == 403
            assert pause_mismatch["body"]["error"] == "scheduled job does not belong to project"

            persisted_active = app.scheduler_service.get_job(owner_id=1, job_id=active_job.job_id)
            assert persisted_active is not None
            assert persisted_active.enabled is True
            assert persisted_active.last_status == "idle"

            resume_mismatch = await _playwright_run_code_json(
                session_name,
                _miniapp_scheduler_fetch_script(
                    "/api/v1/scheduler/jobs/resume",
                    init_data,
                    {"project_slug": beta_project.slug, "job_id": paused_job.job_id},
                ),
            )
            assert resume_mismatch["status"] == 403
            assert resume_mismatch["body"]["error"] == "scheduled job does not belong to project"

            persisted_paused = app.scheduler_service.get_job(owner_id=1, job_id=paused_job.job_id)
            assert persisted_paused is not None
            assert persisted_paused.enabled is False
            assert persisted_paused.last_status == "paused"

            pause_owned = await _playwright_run_code_json(
                session_name,
                _miniapp_scheduler_fetch_script(
                    "/api/v1/scheduler/jobs/pause",
                    init_data,
                    {"project_slug": alpha_project.slug, "job_id": active_job.job_id},
                ),
            )
            assert pause_owned["status"] == 200
            assert pause_owned["body"]["job"]["enabled"] is False
            assert pause_owned["body"]["job"]["project_slug"] == alpha_project.slug

            resume_owned = await _playwright_run_code_json(
                session_name,
                _miniapp_scheduler_fetch_script(
                    "/api/v1/scheduler/jobs/resume",
                    init_data,
                    {"project_slug": alpha_project.slug, "job_id": paused_job.job_id},
                ),
            )
            assert resume_owned["status"] == 200
            assert resume_owned["body"]["job"]["enabled"] is True
            assert resume_owned["body"]["job"]["project_slug"] == alpha_project.slug

            persisted_active_after_pause = app.scheduler_service.get_job(owner_id=1, job_id=active_job.job_id)
            assert persisted_active_after_pause is not None
            assert persisted_active_after_pause.enabled is False
            assert persisted_active_after_pause.last_status == "paused"

            persisted_paused_after_resume = app.scheduler_service.get_job(owner_id=1, job_id=paused_job.job_id)
            assert persisted_paused_after_resume is not None
            assert persisted_paused_after_resume.enabled is True
        finally:
            try:
                await _run_playwright(session_name, "close")
            except AssertionError:
                pass
            await server.close()

    asyncio.run(_run())


def test_webhooks_enabled_restart_required_web(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path, token="t")
        app = BotApp(cfg)

        web_app = web.Application()
        MiniAppRoutes(app).register(web_app)

        server = TestServer(web_app)
        await server.start_server()
        session_name = f"miniapp-webhooks-reload-{uuid.uuid4().hex[:8]}"
        try:
            base_url = str(server.make_url("/"))
            init_data = _build_init_data("t", 1)

            await _run_playwright(session_name, "open", "about:blank", "--browser=chrome")
            boot = await _playwright_run_code_json(
                session_name,
                _miniapp_boot_script(base_url, init_data),
            )
            assert boot == {"title": "cli-proxy MiniApp", "hasBody": True}
            assert "webhooks.enabled" in AppRuntimeService.RESTART_REQUIRED_FIELDS

            ui_state = await _playwright_run_code_json(
                session_name,
                _miniapp_webhooks_ui_toggle_script(),
            )
            assert ui_state["configTabActive"] is True
            assert ui_state["before"]["hint"] == "restart required"

            view = await _playwright_run_code_json(
                session_name,
                _miniapp_json_fetch_script("/api/config/view", init_data),
            )
            assert view["status"] == 200
            assert bool(view["body"]["config"]["webhooks"]["enabled"]) is bool(ui_state["before"]["checked"])
            revision = str(view["body"]["revision"])
            draft = dict(view["body"]["config"])
            draft["webhooks"] = dict(draft.get("webhooks") or {})
            draft["webhooks"]["enabled"] = bool(ui_state["after"]["checked"])

            save = await _playwright_run_code_json(
                session_name,
                _miniapp_json_fetch_script(
                    "/api/config/save",
                    init_data,
                    method="POST",
                    payload={"draft": draft, "expected_revision": revision},
                ),
            )
            assert save["status"] == 200
            assert save["body"]["ok"] is True
            assert "webhooks.enabled" in save["body"]["reload"]["restart_required"]
            assert "webhooks.enabled" not in save["body"]["reload"]["applied"]
            assert save["body"]["reload"]["warnings"] == ["Some changes require process restart."]
            assert app.config.webhooks.enabled is bool(ui_state["before"]["checked"])
        finally:
            try:
                await _run_playwright(session_name, "close")
            except AssertionError:
                pass
            await server.close()

    asyncio.run(_run())


def test_miniapp_config_secret_safe_flow_playwright(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path, token="playwright-secret-telegram-token")
        real_secrets = {
            "telegram.token": cfg.telegram.token,
            "defaults.openai_api_key": "playwright-secret-openai",
            "defaults.zai_api_key": "playwright-secret-zai",
            "defaults.github_token": "playwright-secret-github",
            "defaults.tavily_api_key": "playwright-secret-tavily",
            "defaults.jina_api_key": "playwright-secret-jina",
            "defaults.gemini_oauth_client_secret": "playwright-secret-gemini",
            "webhooks.secret_token": "playwright-secret-webhooks",
            "mcp.token": "playwright-secret-mcp",
        }
        cfg.defaults.openai_api_key = real_secrets["defaults.openai_api_key"]
        cfg.defaults.zai_api_key = real_secrets["defaults.zai_api_key"]
        cfg.defaults.github_token = real_secrets["defaults.github_token"]
        cfg.defaults.tavily_api_key = real_secrets["defaults.tavily_api_key"]
        cfg.defaults.jina_api_key = real_secrets["defaults.jina_api_key"]
        cfg.defaults.gemini_oauth_client_secret = real_secrets["defaults.gemini_oauth_client_secret"]
        cfg.webhooks.secret_token = real_secrets["webhooks.secret_token"]
        cfg.mcp.token = real_secrets["mcp.token"]
        _write_config_file(cfg)

        app = BotApp(cfg)

        async def _reload_runtime_config() -> dict:
            app.config = load_config(cfg.path)
            return {
                "status": "success",
                "applied": [],
                "restart_required": [],
                "warnings": [],
            }

        app.reload_runtime_config = AsyncMock(side_effect=_reload_runtime_config)

        web_app = web.Application()
        MiniAppRoutes(app).register(web_app)

        server = TestServer(web_app)
        await server.start_server()
        session_name = f"miniapp-secret-safe-{uuid.uuid4().hex[:8]}"
        try:
            base_url = str(server.make_url("/"))
            init_data = _build_init_data(cfg.telegram.token, 1)

            await _run_playwright(session_name, "open", "about:blank", "--browser=chrome")
            boot = await _playwright_run_code_json(
                session_name,
                _miniapp_boot_script(base_url, init_data),
            )
            assert boot == {"title": "cli-proxy MiniApp", "hasBody": True}

            unchanged = await _playwright_run_code_json(
                session_name,
                _miniapp_secret_dom_and_unchanged_save_script(
                    model_value="miniapp-secret-safe-model",
                    real_secrets=list(real_secrets.values()),
                ),
            )
            assert unchanged["realSecretsInDom"] == []
            inputs_by_path = {item["path"]: item for item in unchanged["inputsBefore"]}
            assert inputs_by_path["defaults.openai_api_key"]["type"] == "password"
            assert inputs_by_path["defaults.openai_api_key"]["value"] == ""
            assert not any("secret-поля" in item["message"] for item in unchanged["dialogs"])
            assert any("Сохранить config.yaml" in item["message"] for item in unchanged["dialogs"])
            assert unchanged["saveStatus"] == 200
            assert unchanged["saveBody"]["secret_changed"] == []
            assert unchanged["saveBody"]["diff"]["secret_changed"] == []

            saved_after_unchanged = yaml.safe_load(Path(cfg.path).read_text(encoding="utf-8"))
            assert saved_after_unchanged["defaults"]["openai_api_key"] == real_secrets["defaults.openai_api_key"]
            assert saved_after_unchanged["defaults"]["openai_model"] == "miniapp-secret-safe-model"

            new_secret = "playwright-secret-openai-new"
            changed = await _playwright_run_code_json(
                session_name,
                _miniapp_changed_secret_save_script(new_secret=new_secret),
            )
            assert changed["filledValue"] == new_secret
            assert changed["saveStatus"] == 200
            assert any(
                "secret-поля" in item["message"] and "defaults.openai_api_key" in item["message"]
                for item in changed["dialogs"]
            )
            assert any("Сохранить config.yaml" in item["message"] for item in changed["dialogs"])
            assert "defaults.openai_api_key" in changed["saveBody"]["secret_changed"]
            assert "defaults.openai_api_key" in changed["saveBody"]["diff"]["secret_changed"]

            saved_after_changed = yaml.safe_load(Path(cfg.path).read_text(encoding="utf-8"))
            assert saved_after_changed["defaults"]["openai_api_key"] == new_secret
        finally:
            try:
                await _run_playwright(session_name, "close")
            except AssertionError:
                pass
            await server.close()

    asyncio.run(_run())


def test_malformed_json_400_web(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path, token="t")
        app = BotApp(cfg)
        workdir = tmp_path / "playwright-workdir"
        workdir.mkdir()
        session = app.manager.create(1, "dummy", str(workdir))
        session_uid = session_runtime_uid(session)

        web_app = web.Application()
        MiniAppRoutes(app).register(web_app)

        server = TestServer(web_app)
        await server.start_server()
        session_name = f"miniapp-malformed-json-{uuid.uuid4().hex[:8]}"
        try:
            base_url = str(server.make_url("/"))
            init_data = _build_init_data("t", 1)

            await _run_playwright(session_name, "open", "about:blank", "--browser=chrome")
            boot = await _playwright_run_code_json(
                session_name,
                _miniapp_boot_script(base_url, init_data),
            )
            assert boot == {"title": "cli-proxy MiniApp", "hasBody": True}

            malformed_paths = [
                "/api/config/validate",
                "/api/config/diff",
                "/api/config/save",
                "/api/files/write",
                "/api/files/create",
                "/api/files/delete",
            ]
            for path in malformed_paths:
                response = await _playwright_run_code_json(
                    session_name,
                    _miniapp_raw_json_fetch_script(path, init_data, body="{"),
                )
                assert response["status"] == 400
                assert response["body"] == {"ok": False, "error": "invalid json body"}

            non_object = await _playwright_run_code_json(
                session_name,
                _miniapp_raw_json_fetch_script("/api/config/diff", init_data, body="[]"),
            )
            assert non_object["status"] == 400
            assert non_object["body"] == {"ok": False, "error": "request body must be an object"}

            happy_path = await _playwright_run_code_json(
                session_name,
                _miniapp_raw_json_fetch_script(
                    "/api/files/create",
                    init_data,
                    body=json.dumps(
                        {"session_uid": session_uid, "path": "playwright.txt", "kind": "file"},
                        ensure_ascii=False,
                    ),
                ),
            )
            assert happy_path["status"] == 200
            assert happy_path["body"]["ok"] is True
            assert (workdir / "playwright.txt").exists() is True
        finally:
            try:
                await _run_playwright(session_name, "close")
            except AssertionError:
                pass
            await server.close()

    asyncio.run(_run())


def test_miniapp_settings_ui_smoke_playwright(tmp_path) -> None:
    """Playwright smoke test: Settings UI elements exist in HTML."""
    async def _run() -> None:
        cfg = _build_config(tmp_path, token="t")
        app = BotApp(cfg)
        workdir = tmp_path / "settings-smoke"
        workdir.mkdir()
        app.manager.create(1, "dummy", str(workdir))

        web_app = web.Application()
        MiniAppRoutes(app).register(web_app)

        server = TestServer(web_app)
        await server.start_server()
        session_name = f"miniapp-settings-smoke-{uuid.uuid4().hex[:8]}"
        try:
            base_url = str(server.make_url("/"))
            init_data = _build_init_data("t", 1)

            await _run_playwright(session_name, "open", "about:blank", "--browser=chrome")
            boot = await _playwright_run_code_json(
                session_name,
                _miniapp_boot_script(base_url, init_data),
            )
            assert boot == {"title": "cli-proxy MiniApp", "hasBody": True}

            # Check Settings UI elements exist in HTML
            check_script = (
                "async page => {"
                "  const html = await page.content();"
                "  return {"
                "    hasSettingsTab: html.includes('data-tab=\"settings\"'),"
                "    hasSettingsSession: html.includes('id=\"settingsSession\"'),"
                "    hasSettingsActiveMode: html.includes('id=\"settingsActiveMode\"'),"
                "    hasRemoteControlEnabled: html.includes('id=\"settingsRemoteControlEnabled\"'),"
                "    hasRemoteControlHostField: html.includes('id=\"settingsRemoteControlHostField\"'),"
                "    hasRemoteControlHost: html.includes('id=\"settingsRemoteControlHost\"'),"
                "    hasRemoteControlRecheck: html.includes('id=\"settingsRemoteControlRecheck\"'),"
                "    hasRemoteControlError: html.includes('id=\"settingsRemoteControlError\"')"
                "  };"
                "}"
            )

            ui_check = await _playwright_run_code_json(session_name, check_script)
            assert ui_check["hasSettingsTab"] is True
            assert ui_check["hasSettingsSession"] is True
            assert ui_check["hasSettingsActiveMode"] is True
            assert ui_check["hasRemoteControlEnabled"] is True
            assert ui_check["hasRemoteControlHostField"] is True
            assert ui_check["hasRemoteControlHost"] is True
            assert ui_check["hasRemoteControlRecheck"] is True
            assert ui_check["hasRemoteControlError"] is True

        finally:
            try:
                await _run_playwright(session_name, "close")
            except AssertionError:
                pass
            await server.close()

    asyncio.run(_run())


def test_miniapp_settings_active_mode_toggle_playwright(tmp_path) -> None:
    """Playwright test: Active Mode select can enable SDD and clear it back to direct CLI."""
    async def _run() -> None:
        cfg = _build_config(tmp_path, token="t")
        app = BotApp(cfg)
        workdir = tmp_path / "settings-active-mode"
        workdir.mkdir()
        session = app.manager.create(1, "dummy", str(workdir))

        web_app = web.Application()
        MiniAppRoutes(app).register(web_app)

        server = TestServer(web_app)
        await server.start_server()
        session_name = f"miniapp-settings-mode-{uuid.uuid4().hex[:8]}"
        try:
            base_url = str(server.make_url("/"))
            init_data = _build_init_data("t", 1)

            await _run_playwright(session_name, "open", "about:blank", "--browser=chrome")
            boot = await _playwright_run_code_json(
                session_name,
                _miniapp_boot_script(base_url, init_data),
            )
            assert boot == {"title": "cli-proxy MiniApp", "hasBody": True}

            toggle_script = (
                "async page => {"
                "  await page.waitForSelector('.tabs button[data-tab=\"settings\"]', { state: 'attached', timeout: 20000 });"
                "  await page.evaluate(() => document.querySelector('.tabs button[data-tab=\"settings\"]')?.click());"
                "  await page.waitForSelector('#settingsActiveMode', { state: 'attached', timeout: 20000 });"
                "  await page.waitForFunction(() => {"
                "    const select = document.getElementById('settingsSession');"
                "    return select && Array.from(select.options).some((option) => option.value);"
                "  }, null, { timeout: 10000 });"
                "  await page.evaluate(() => {"
                "    const select = document.getElementById('settingsSession');"
                "    if (!select.value) {"
                "      const option = Array.from(select.options).find((item) => item.value);"
                "      if (option) select.value = option.value;"
                "    }"
                "    select.dispatchEvent(new Event('change', { bubbles: true }));"
                "  });"
                "  await page.waitForFunction(() => {"
                "    const select = document.getElementById('settingsActiveMode');"
                "    return select && Array.from(select.options).some((option) => option.value === 'sdd');"
                "  }, null, { timeout: 10000 });"
                "  await page.selectOption('#settingsActiveMode', 'sdd');"
                "  await page.click('#settingsSave');"
                "  await page.waitForFunction(() => document.getElementById('settingsActiveMode')?.value === 'sdd',"
                "    null, { timeout: 10000 });"
                "  const afterOn = await page.$eval('#settingsActiveMode', (el) => el.value);"
                "  await page.selectOption('#settingsActiveMode', '');"
                "  await page.click('#settingsSave');"
                "  await page.waitForFunction(() => document.getElementById('settingsActiveMode')?.value === '',"
                "    null, { timeout: 10000 });"
                "  const afterOff = await page.$eval('#settingsActiveMode', (el) => el.value);"
                "  return { afterOn, afterOff };"
                "}"
            )
            result = await _playwright_run_code_json(session_name, toggle_script)

            assert result == {"afterOn": "sdd", "afterOff": ""}
            assert session.modes.active_mode is None

        finally:
            try:
                await _run_playwright(session_name, "close")
            except AssertionError:
                pass
            await server.close()

    asyncio.run(_run())


def test_miniapp_settings_rc_toggle_playwright(tmp_path) -> None:
    """Playwright test: Remote Control checkbox can be toggled."""
    async def _run() -> None:
        cfg = _build_config(tmp_path, token="t")
        app = BotApp(cfg)
        workdir = tmp_path / "settings-toggle"
        workdir.mkdir()
        app.manager.create(1, "dummy", str(workdir))

        web_app = web.Application()
        MiniAppRoutes(app).register(web_app)

        server = TestServer(web_app)
        await server.start_server()
        session_name = f"miniapp-settings-toggle-{uuid.uuid4().hex[:8]}"
        try:
            base_url = str(server.make_url("/"))
            init_data = _build_init_data("t", 1)

            await _run_playwright(session_name, "open", "about:blank", "--browser=chrome")

            # First boot the page
            boot = await _playwright_run_code_json(
                session_name,
                _miniapp_boot_script(base_url, init_data),
            )
            assert boot == {"title": "cli-proxy MiniApp", "hasBody": True}

            # Wait for page to load using eval (wait for attached, not visible)
            wait_script = (
                "async page => {"
                "  await page.waitForSelector('#settingsRemoteControlEnabled',"
                "    { timeout: 10000, state: 'attached' });"
                "  return { loaded: true };"
                "}"
            )
            await _playwright_run_code_json(session_name, wait_script)

            # Toggle RC checkbox via script
            toggle_script = (
                "async page => {"
                "  return await page.evaluate(() => {"
                "    const rcEnabled = document.getElementById('settingsRemoteControlEnabled');"
                "    if (!rcEnabled) return { error: 'RC checkbox not found' };"
                "    const before = rcEnabled.checked;"
                "    rcEnabled.click();"
                "    return new Promise(resolve => {"
                "      setTimeout(() => {"
                "        const after = rcEnabled.checked;"
                "        const hostField = document.getElementById('settingsRemoteControlHostField');"
                "        resolve({"
                "          before,"
                "          after,"
                "          changed: before !== after,"
                "          hostFieldVisible: hostField ? hostField.style.display !== 'none' : false"
                "        });"
                "      }, 300);"
                "    });"
                "  });"
                "}"
            )

            toggle_result = await _playwright_run_code_json(session_name, toggle_script)
            assert "error" not in toggle_result
            assert toggle_result["changed"] is True

        finally:
            try:
                await _run_playwright(session_name, "close")
            except AssertionError:
                pass
            await server.close()

    asyncio.run(_run())


def test_miniapp_conflict_dialog_elements_playwright(tmp_path) -> None:
    """Playwright test: Conflict dialog elements exist in HTML and are hidden by default."""
    async def _run() -> None:
        cfg = _build_config(tmp_path, token="t")
        app = BotApp(cfg)
        workdir = tmp_path / "conflict-test"
        workdir.mkdir()
        app.manager.create(1, "dummy", str(workdir))

        web_app = web.Application()
        MiniAppRoutes(app).register(web_app)

        server = TestServer(web_app)
        await server.start_server()
        session_name = f"miniapp-conflict-dialog-{uuid.uuid4().hex[:8]}"
        try:
            base_url = str(server.make_url("/"))
            init_data = _build_init_data("t", 1)

            await _run_playwright(session_name, "open", "about:blank", "--browser=chrome")

            # First boot the page
            boot = await _playwright_run_code_json(
                session_name,
                _miniapp_boot_script(base_url, init_data),
            )
            assert boot == {"title": "cli-proxy MiniApp", "hasBody": True}

            # Wait for page to load using eval (wait for attached, not visible)
            wait_script = (
                "async page => {"
                "  await page.waitForSelector('#ace', { timeout: 10000, state: 'attached' });"
                "  return { loaded: true };"
                "}"
            )
            await _playwright_run_code_json(session_name, wait_script)

            # Check conflict dialog elements exist in HTML
            check_script = (
                "async page => {"
                "  return await page.evaluate(() => {"
                "    const html = document.documentElement.outerHTML;"
                "    const forceSave = document.getElementById('editorForceSave');"
                "    const conflictForceSave = document.getElementById('editorConflictForceSave');"
                "    const conflictDialog = document.getElementById('editorConflictDialog');"
                "    return {"
                "      hasConflictDialog: html.includes('id=\"editorConflictDialog\"'),"
                "      hasConflictDiff: html.includes('id=\"editorConflictDiff\"'),"
                "      hasConflictForceSave: html.includes('id=\"editorConflictForceSave\"'),"
                "      hasConflictMessage: html.includes('Обнаружен конфликт версий!'),"
                "      hasForceSave: html.includes('id=\"editorForceSave\"'),"
                "      forceSaveHidden: forceSave && forceSave.style.display === 'none',"
                "      conflictDialogHidden: conflictDialog && conflictDialog.style.display === 'none',"
                "      conflictForceSaveHidden: conflictForceSave && conflictForceSave.style.display === 'none'"
                "    };"
                "  });"
                "}"
            )

            editor_state = await _playwright_run_code_json(session_name, check_script)
            assert editor_state["hasConflictDialog"] is True
            assert editor_state["hasConflictDiff"] is True
            assert editor_state["hasConflictForceSave"] is True
            assert editor_state["hasConflictMessage"] is True
            assert editor_state["hasForceSave"] is True
            assert editor_state["forceSaveHidden"] is True
            assert editor_state["conflictDialogHidden"] is True
            # conflictForceSave button is inside hidden dialog, so it's effectively hidden

        finally:
            try:
                await _run_playwright(session_name, "close")
            except AssertionError:
                pass
            await server.close()

    asyncio.run(_run())


def test_miniapp_conflict_diff_dialog_functionality(tmp_path) -> None:
    """Playwright test: Conflict diff dialog shows diff content and message."""
    async def _run() -> None:
        cfg = _build_config(tmp_path, token="t")
        app = BotApp(cfg)
        workdir = tmp_path / "conflict-diff"
        workdir.mkdir()
        app.manager.create(1, "dummy", str(workdir))

        web_app = web.Application()
        MiniAppRoutes(app).register(web_app)

        server = TestServer(web_app)
        await server.start_server()
        session_name = f"miniapp-conflict-diff-{uuid.uuid4().hex[:8]}"
        try:
            base_url = str(server.make_url("/"))
            init_data = _build_init_data("t", 1)

            await _run_playwright(session_name, "open", "about:blank", "--browser=chrome")

            # Boot the page
            boot = await _playwright_run_code_json(
                session_name,
                _miniapp_boot_script(base_url, init_data),
            )
            assert boot == {"title": "cli-proxy MiniApp", "hasBody": True}

            # Wait for page to load
            wait_script = (
                "async page => {"
                "  await page.waitForSelector('#ace', { timeout: 10000, state: 'attached' });"
                "  return { loaded: true };"
                "}"
            )
            await _playwright_run_code_json(session_name, wait_script)

            # Check diff dialog has proper structure for showing diff
            diff_check_script = (
                "async page => {"
                "  return await page.evaluate(() => {"
                "    const dialog = document.getElementById('editorConflictDialog');"
                "    const diffPre = document.getElementById('editorConflictDiff');"
                "    const forceSaveBtn = document.getElementById('editorConflictForceSave');"
                "    const cancelBtn = document.getElementById('editorConflictCancel');"
                "    const reloadBtn = document.getElementById('editorConflictReload');"
                "    return {"
                "      dialogExists: !!dialog,"
                "      diffPreExists: !!diffPre,"
                "      diffPreHasMonospaceFont: diffPre && window.getComputedStyle(diffPre).fontFamily.includes('monospace'),"
                "      forceSaveBtnExists: !!forceSaveBtn,"
                "      forceSaveBtnHasDangerStyle: forceSaveBtn && forceSaveBtn.classList.contains('btn-danger'),"
                "      cancelBtnExists: !!cancelBtn,"
                "      reloadBtnExists: !!reloadBtn,"
                "      dialogHiddenByDefault: dialog && dialog.style.display === 'none'"
                "    };"
                "  });"
                "}"
            )

            diff_state = await _playwright_run_code_json(session_name, diff_check_script)
            assert diff_state["dialogExists"] is True
            assert diff_state["diffPreExists"] is True
            assert diff_state["diffPreHasMonospaceFont"] is True
            assert diff_state["forceSaveBtnExists"] is True
            assert diff_state["forceSaveBtnHasDangerStyle"]  # Should have danger style (var(--danger))
            assert diff_state["cancelBtnExists"] is True
            assert diff_state["reloadBtnExists"] is True
            assert diff_state["dialogHiddenByDefault"] is True

        finally:
            try:
                await _run_playwright(session_name, "close")
            except AssertionError:
                pass
            await server.close()

    asyncio.run(_run())


def test_miniapp_settings_rc_preflight_ui_elements(tmp_path) -> None:
    """Playwright test: Settings UI has all elements for preflight error display."""
    async def _run() -> None:
        cfg = _build_config(tmp_path, token="t")
        app = BotApp(cfg)
        workdir = tmp_path / "settings-preflight"
        workdir.mkdir()
        app.manager.create(1, "dummy", str(workdir))

        web_app = web.Application()
        MiniAppRoutes(app).register(web_app)

        server = TestServer(web_app)
        await server.start_server()
        session_name = f"miniapp-preflight-ui-{uuid.uuid4().hex[:8]}"
        try:
            base_url = str(server.make_url("/"))
            init_data = _build_init_data("t", 1)

            await _run_playwright(session_name, "open", "about:blank", "--browser=chrome")

            # Boot the page
            boot = await _playwright_run_code_json(
                session_name,
                _miniapp_boot_script(base_url, init_data),
            )
            assert boot == {"title": "cli-proxy MiniApp", "hasBody": True}

            # Wait for settings to load
            wait_script = (
                "async page => {"
                "  await page.waitForSelector('#settingsRemoteControlEnabled', { timeout: 10000, state: 'attached' });"
                "  return { loaded: true };"
                "}"
            )
            await _playwright_run_code_json(session_name, wait_script)

            # Check preflight error UI elements exist
            preflight_check_script = (
                "async page => {"
                "  return await page.evaluate(() => {"
                "    const rcError = document.getElementById('settingsRemoteControlError');"
                "    const rcHostSelect = document.getElementById('settingsRemoteControlHost');"
                "    const rcRecheck = document.getElementById('settingsRemoteControlRecheck');"
                "    return {"
                "      errorDivExists: !!rcError,"
                "      hostSelectExists: !!rcHostSelect,"
                "      recheckButtonExists: !!rcRecheck,"
                "      errorDivHiddenByDefault: rcError && rcError.style.display === 'none'"
                "    };"
                "  });"
                "}"
            )

            preflight_state = await _playwright_run_code_json(session_name, preflight_check_script)
            assert preflight_state["errorDivExists"] is True
            assert preflight_state["hostSelectExists"] is True
            assert preflight_state["recheckButtonExists"] is True
            assert preflight_state["errorDivHiddenByDefault"] is True

        finally:
            try:
                await _run_playwright(session_name, "close")
            except AssertionError:
                pass
            await server.close()

    asyncio.run(_run())
