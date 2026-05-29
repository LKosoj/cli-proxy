from __future__ import annotations

import asyncio
import hashlib
import json
import re
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent.cli_routing import run_prompt_routed_meta
from app.services.memory_event_store import MemoryEventStore
from app.services.run_artifact_store import RunArtifactStore
from app.services.skill_policy_service import SkillPolicyService
from app.services.skill_registry_service import SkillRegistryService
from app.services import skill_runtime_service as skill_runtime_module
from app.services.skill_runtime_service import SkillRuntimeService
from app.services.task_bearing_cli_hook_service import get_task_bearing_cli_hook_service
from bot import BotApp
from config import AppConfig, DefaultsConfig, MCPConfig, TelegramConfig, ThreadModeConfig, ToolConfig
from session import SessionManager, session_runtime_uid


class _FakeCompletions:
    def __init__(self, client):
        self._client = client

    async def create(self, *, model, messages, temperature, max_tokens, response_format):
        return await self._client._create(
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format=response_format,
        )


class _FakeChat:
    def __init__(self, client):
        self.completions = _FakeCompletions(client)


class FakeOpenAIClient:
    def __init__(self, response_json: str):
        self.response_json = response_json
        self.calls: list[dict[str, object]] = []
        self.chat = _FakeChat(self)

    async def _create(self, *, model, messages, temperature, max_tokens, response_format):
        self.calls.append(
            {
                "model": model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "response_format": response_format,
            }
        )
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.response_json))]
        )


def _selector_keywords(*parts: str) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for token in re.findall(r"[0-9A-Za-zА-Яа-яЁё][0-9A-Za-zА-Яа-яЁё_:+.-]{1,}", " ".join(parts).lower()):
        cleaned = token.strip("._:-+")
        if len(cleaned) < 3 or cleaned in seen:
            continue
        seen.add(cleaned)
        result.append(cleaned)
    return result[:8]


def _write_skill(root: Path, skill_id: str, *, title: str, description: str) -> Path:
    path = root / skill_id / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = "\n".join(
        [
            "---",
            f"name: {json.dumps(title, ensure_ascii=False)}",
            f"description: {json.dumps(description, ensure_ascii=False)}",
            "---",
            "",
            description,
            "",
        ]
    )
    path.write_text(raw, encoding="utf-8")
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    (path.parent / skill_runtime_module._SELECTOR_METADATA_FILENAME).write_text(
        json.dumps(
            {
                "version": skill_runtime_module._SELECTOR_METADATA_VERSION,
                "skill_md_sha256": f"sha256:{digest}",
                "summary": description,
                "keywords": _selector_keywords(skill_id, title, description),
                "specificity": "generic",
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return path


def _build_bot_config(tmp_path: Path, *, intent: str) -> AppConfig:
    workdir = tmp_path / f"bot_workdir_{intent}"
    runtime = tmp_path / f"runtime_{intent}"
    workdir.mkdir(parents=True, exist_ok=True)
    runtime.mkdir(parents=True, exist_ok=True)
    return AppConfig(
        telegram=TelegramConfig(token="", whitelist_chat_ids=[1], admlist_chat_ids=[1]),
        tools={
            "dummy": ToolConfig(
                name="dummy",
                mode="headless",
                cmd=["bash", "-lc", "cat"],
            )
        },
        defaults=DefaultsConfig(
            workdir=str(workdir),
            state_path=str(runtime / "state.json"),
            toolhelp_path=str(runtime / "toolhelp.json"),
            log_path=str(runtime / "bot.log"),
            openai_api_key="test-key",
            openai_model="small-model",
            openai_big_model="big-model",
            run_artifacts_enabled=True,
            run_metrics_enabled=True,
            skill_discovery_mode="suggest",
            skill_registry_paths=[".cli-proxy/skills"],
            skill_allowlisted_sources=["local:global-registry", "local:project-registry"],
        ),
        mcp=MCPConfig(enabled=False),
        mcp_clients=[],
        presets=[],
        path=str(tmp_path / f"config_{intent}.yaml"),
        thread_mode=ThreadModeConfig(enabled=False, mode="private"),
    )


def _build_routing_config(tmp_path: Path, *, intent: str) -> AppConfig:
    workdir = tmp_path / f"routing_workdir_{intent}"
    runtime = tmp_path / f"routing_runtime_{intent}"
    workdir.mkdir(parents=True, exist_ok=True)
    runtime.mkdir(parents=True, exist_ok=True)
    tools = {
        "gemini": ToolConfig(name="gemini", mode="headless", cmd=["bash", "-lc", "cat"]),
        "claude": ToolConfig(name="claude", mode="headless", cmd=["bash", "-lc", "cat"]),
        "qwen": ToolConfig(name="qwen", mode="headless", cmd=["bash", "-lc", "cat"]),
        "codex": ToolConfig(name="codex", mode="headless", cmd=["bash", "-lc", "cat"]),
    }
    return AppConfig(
        telegram=TelegramConfig(token="", whitelist_chat_ids=[1], admlist_chat_ids=[1]),
        tools=tools,
        defaults=DefaultsConfig(
            workdir=str(workdir),
            state_path=str(runtime / "state.json"),
            toolhelp_path=str(runtime / "toolhelp.json"),
            log_path=str(runtime / "bot.log"),
            default_cli="claude",
            openai_api_key="test-key",
            openai_model="small-model",
            openai_big_model="big-model",
            run_artifacts_enabled=True,
            run_metrics_enabled=True,
            skill_discovery_mode="suggest",
            skill_registry_paths=[".cli-proxy/skills"],
            skill_allowlisted_sources=["local:global-registry", "local:project-registry"],
        ),
        mcp=MCPConfig(enabled=False),
        mcp_clients=[],
        presets=[],
        path=str(tmp_path / f"routing_config_{intent}.yaml"),
    )


def _make_update(*, chat_id: int, text: str, user_id: int = 1):
    message = SimpleNamespace(
        text=text,
        message_thread_id=None,
        document=None,
        photo=None,
        video=None,
        audio=None,
        voice=None,
        sticker=None,
        animation=None,
        video_note=None,
        caption="",
        media_group_id=None,
    )
    return SimpleNamespace(
        effective_chat=SimpleNamespace(id=int(chat_id)),
        effective_user=SimpleNamespace(id=int(user_id)),
        effective_message=message,
        message=message,
    )


def _bind_shared_skill_runtime(config: AppConfig, client: FakeOpenAIClient) -> SkillRuntimeService:
    service = SkillRuntimeService(
        config,
        registry_service=SkillRegistryService(config),
        policy_service=SkillPolicyService(config),
        client_factory=lambda: client,
    )
    setattr(config, "_shared_skill_runtime_selector_service", service)
    get_task_bearing_cli_hook_service(config, skill_runtime=service)
    return service


def _read_events(path: str) -> list[dict]:
    return [
        json.loads(line)
        for line in Path(path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


@pytest.mark.asyncio
async def test_direct_send_applies_skill_selection_and_records_events(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_BIG_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    cfg = _build_bot_config(tmp_path, intent="send")
    global_skills = Path(cfg.defaults.workdir) / ".cli-proxy" / "skills"
    _write_skill(global_skills, "playwright-cli", title="Playwright CLI", description="browser testing skill")

    app = BotApp(cfg)
    try:
        client = FakeOpenAIClient('{"selected_skill_ids":["playwright-cli"]}')
        _bind_shared_skill_runtime(cfg, client)
        project_dir = tmp_path / "project_send"
        project_dir.mkdir(parents=True, exist_ok=True)
        session = app.manager.create(1, "dummy", str(project_dir))
        app.send_output = AsyncMock(return_value=None)
        scheduled: list[asyncio.Task] = []

        def _capture_task(coro, *, label: str):
            _ = label
            task = asyncio.create_task(coro)
            scheduled.append(task)
            return task

        def _capture_start_prompt_task(session_obj, prompt, dest, context, *, task_name="run_prompt"):
            _ = task_name
            task = asyncio.create_task(app.session_management.run_prompt(session_obj, prompt, dest, context))
            scheduled.append(task)
            return True

        monkeypatch.setattr(app.session_management, "start_prompt_task", _capture_start_prompt_task)

        captured: dict[str, str] = {}
        completed = asyncio.Event()

        async def _fake_run_prompt(prompt: str, **_kwargs):
            captured["prompt"] = str(prompt)
            completed.set()
            return "OUT:ok"

        session.run_prompt = _fake_run_prompt
        await app.handlers.cmd_send(
            _make_update(chat_id=1, text="/send Проверь браузерную форму"),
            SimpleNamespace(args=["Проверь", "браузерную", "форму"], bot=SimpleNamespace()),
        )

        assert scheduled
        await asyncio.wait_for(asyncio.gather(*scheduled), timeout=5.0)
        assert completed.is_set() is True

        assert "playwright-cli" in captured["prompt"]
        assert "Исходная задача:" in captured["prompt"]

        run = RunArtifactStore(cfg).latest_run(session=session, mode_id="cli")
        assert run is not None
        events = _read_events(run.events_path)
        assert any(
            item.get("event_type") == "cli_execution_start" and item.get("source") == "telegram_direct"
            for item in events
        )
        assert any(
            item.get("event_type") == "cli_skill_context_applied"
            and item.get("selected_skill_ids") == ["playwright-cli"]
            for item in events
        )
        assert len(client.calls) == 1
    finally:
        app.shutdown_html_process_pool()


@pytest.mark.asyncio
async def test_programmatic_technical_commands_bypass_selector_and_keep_prompt_verbatim(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_BIG_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    cfg = _build_bot_config(tmp_path, intent="technical_bypass")
    global_skills = Path(cfg.defaults.workdir) / ".cli-proxy" / "skills"
    _write_skill(global_skills, "playwright-cli", title="Playwright CLI", description="browser testing skill")

    app = BotApp(cfg)
    try:
        client = FakeOpenAIClient('{"selected_skill_ids":["playwright-cli"]}')
        _bind_shared_skill_runtime(cfg, client)
        project_dir = tmp_path / "project_raw"
        project_dir.mkdir(parents=True, exist_ok=True)
        session = app.manager.create(1, "dummy", str(project_dir))
        captured: dict[str, str] = {}

        async def _fake_run_prompt(prompt: str, **_kwargs):
            captured["prompt"] = str(prompt)
            return "OUT:raw"

        session.run_prompt = _fake_run_prompt
        out = await app.run_prompt_raw(
            "git status",
            session_id=session.id,
            chat_id=1,
            technical_command=True,
        )

        assert out == "OUT:raw"
        assert captured["prompt"] == "git status"
        assert client.calls == []

        run = RunArtifactStore(cfg).latest_run(session=session, mode_id="cli")
        assert run is not None
        events = _read_events(run.events_path)
        assert any(
            item.get("event_type") == "cli_execution_start"
            and item.get("bypass_reason") == "technical_command"
            for item in events
        )
    finally:
        app.shutdown_html_process_pool()


@pytest.mark.asyncio
async def test_memory_events_shadow_capture_records_raw_prompt_run_without_prompt_text(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_BIG_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    cfg = _build_bot_config(tmp_path, intent="memory_events")
    cfg.defaults.memory_events_enabled = True
    app = BotApp(cfg)
    try:
        project_dir = tmp_path / "project_memory_events"
        project_dir.mkdir(parents=True, exist_ok=True)
        session = app.manager.create(1, "dummy", str(project_dir))

        async def _fake_run_prompt(prompt: str, **_kwargs):
            assert prompt == "echo OPENAI_API_KEY=sk-secret-for-memory"
            return "OUT:memory"

        session.run_prompt = _fake_run_prompt
        out = await app.run_prompt_raw(
            "echo OPENAI_API_KEY=sk-secret-for-memory",
            session_id=session.id,
            chat_id=1,
            technical_command=True,
        )

        assert out == "OUT:memory"
        store = MemoryEventStore.from_config(cfg)
        rows = store.list_events(limit=10)
        event_types = [item.event_type for item in rows]
        assert "cli_execution_start" in event_types
        assert "cli_execution_end" in event_types
        start = next(item for item in rows if item.event_type == "cli_execution_start")
        end = next(item for item in rows if item.event_type == "cli_execution_end")
        assert start.source == "raw_prompt"
        assert start.mode_id == "cli"
        assert start.payload["prompt_len"] == len("echo OPENAI_API_KEY=sk-secret-for-memory")
        assert "prompt" not in start.payload
        assert "sk-secret-for-memory" not in json.dumps(start.payload, ensure_ascii=False)
        assert end.payload["status"] == "ok"
        assert end.payload["output_len"] == len("OUT:memory")
        assert start.run_id == end.run_id
        assert start.session_uid == end.session_uid
    finally:
        app.shutdown_html_process_pool()


@pytest.mark.asyncio
async def test_memory_events_disabled_keeps_shadow_store_empty(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_BIG_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    cfg = _build_bot_config(tmp_path, intent="memory_events_disabled")
    app = BotApp(cfg)
    try:
        project_dir = tmp_path / "project_memory_events_disabled"
        project_dir.mkdir(parents=True, exist_ok=True)
        session = app.manager.create(1, "dummy", str(project_dir))

        async def _fake_run_prompt(prompt: str, **_kwargs):
            return f"OUT:{prompt}"

        session.run_prompt = _fake_run_prompt
        out = await app.run_prompt_raw(
            "git status",
            session_id=session.id,
            chat_id=1,
            technical_command=True,
        )

        assert out == "OUT:git status"
        assert MemoryEventStore.from_config(cfg).list_events(limit=10) == []
    finally:
        app.shutdown_html_process_pool()


@pytest.mark.asyncio
async def test_memory_events_keep_distinct_runs_without_run_artifacts(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_BIG_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    cfg = _build_bot_config(tmp_path, intent="memory_events_no_artifacts")
    cfg.defaults.memory_events_enabled = True
    cfg.defaults.run_artifacts_enabled = False
    app = BotApp(cfg)
    try:
        project_dir = tmp_path / "project_memory_events_no_artifacts"
        project_dir.mkdir(parents=True, exist_ok=True)
        session = app.manager.create(1, "dummy", str(project_dir))

        async def _fake_run_prompt(prompt: str, **_kwargs):
            return f"OUT:{prompt}"

        session.run_prompt = _fake_run_prompt
        await app.run_prompt_raw("git status", session_id=session.id, chat_id=1, technical_command=True)
        await app.run_prompt_raw("git status", session_id=session.id, chat_id=1, technical_command=True)

        rows = MemoryEventStore.from_config(cfg).list_events(limit=10)
        assert [item.event_type for item in rows].count("cli_execution_start") == 2
        assert [item.event_type for item in rows].count("cli_execution_end") == 2
        assert {item.session_uid for item in rows} == {session_runtime_uid(session)}
    finally:
        app.shutdown_html_process_pool()


@pytest.mark.asyncio
async def test_memory_events_prune_retention_and_do_not_store_error_text(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_BIG_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    cfg = _build_bot_config(tmp_path, intent="memory_events_error")
    cfg.defaults.memory_events_enabled = True
    cfg.defaults.memory_events_retention_days = 1
    store = MemoryEventStore.from_config(cfg)
    old, _inserted = store.record_event(
        event_type="old_event",
        source="test",
        session_uid="old-session",
        dedupe_key="old",
        created_at=1.0,
    )
    app = BotApp(cfg)
    try:
        project_dir = tmp_path / "project_memory_events_error"
        project_dir.mkdir(parents=True, exist_ok=True)
        session = app.manager.create(1, "dummy", str(project_dir))

        async def _fake_run_prompt(prompt: str, **_kwargs):
            raise RuntimeError("command failed with token=secret-value")

        session.run_prompt = _fake_run_prompt
        with pytest.raises(RuntimeError):
            await app.run_prompt_raw(
                "git status",
                session_id=session.id,
                chat_id=1,
                technical_command=True,
            )

        assert MemoryEventStore.from_config(cfg).get_event(old.event_id) is None
        rows = MemoryEventStore.from_config(cfg).list_events(limit=10)
        error = next(item for item in rows if item.event_type == "cli_execution_error")
        assert error.payload["error_type"] == "RuntimeError"
        assert "error" not in error.payload
        dumped = json.dumps(error.payload, ensure_ascii=False)
        assert "secret-value" not in dumped
        assert "command failed" not in dumped
    finally:
        app.shutdown_html_process_pool()


@pytest.mark.asyncio
async def test_memory_events_retry_reason_is_metadata_only(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_BIG_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    cfg = _build_bot_config(tmp_path, intent="memory_events_retry")
    cfg.defaults.memory_events_enabled = True
    manager = SessionManager(cfg)
    project_dir = tmp_path / "project_memory_events_retry"
    project_dir.mkdir(parents=True, exist_ok=True)
    session = manager.create(1, "dummy", str(project_dir))

    service = get_task_bearing_cli_hook_service(cfg)
    prepared = await service.prepare_prompt(
        session=session,
        prompt="git status",
        source="raw_prompt",
        technical_command=True,
    )
    service.record_retry(prepared, reason="retry after token=secret-value")

    rows = MemoryEventStore.from_config(cfg).list_events(limit=10)
    retry = next(item for item in rows if item.event_type == "cli_execution_retry")
    assert retry.payload["reason_type"] == "str"
    assert retry.payload["reason_len"] == len("retry after token=secret-value")
    assert "reason_hash" in retry.payload
    dumped = json.dumps(retry.payload, ensure_ascii=False)
    assert "secret-value" not in dumped
    assert "retry after" not in dumped


@pytest.mark.asyncio
async def test_routed_identical_prompts_reuse_skill_cache(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_BIG_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    cfg = _build_routing_config(tmp_path, intent="cache")
    global_skills = Path(cfg.defaults.workdir) / ".cli-proxy" / "skills"
    _write_skill(global_skills, "playwright-cli", title="Playwright CLI", description="browser testing skill")
    client = FakeOpenAIClient('{"selected_skill_ids":["playwright-cli"]}')
    _bind_shared_skill_runtime(cfg, client)

    manager = SessionManager(cfg)
    project_dir = tmp_path / "project_routed"
    project_dir.mkdir(parents=True, exist_ok=True)
    session = manager.create(1, "codex", str(project_dir))
    prompts: list[str] = []

    async def _fake_run_prompt(self, prompt: str, *args, **kwargs):
        _ = args
        _ = kwargs
        prompts.append(str(prompt))
        return f"ok:{self.tool.name}:{prompt}"

    session.run_prompt = types.MethodType(_fake_run_prompt, session)

    cli_used_one, out_one = await run_prompt_routed_meta(
        session,
        cfg,
        "default",
        "Проверь браузерный сценарий.",
        chat_id=1,
    )
    cli_used_two, out_two = await run_prompt_routed_meta(
        session,
        cfg,
        "default",
        "Проверь браузерный сценарий.",
        chat_id=1,
    )

    assert cli_used_one == "claude"
    assert cli_used_two == "claude"
    assert "playwright-cli" in prompts[0]
    assert prompts[0] == prompts[1]
    assert "playwright-cli" in out_one
    assert "playwright-cli" in out_two
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_cli_routing_subrun_does_not_inherit_active_mode(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_BIG_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    cfg = _build_routing_config(tmp_path, intent="active_mode_route")
    client = FakeOpenAIClient('{"selected_skill_ids":[]}')
    _bind_shared_skill_runtime(cfg, client)

    manager = SessionManager(cfg)
    project_dir = tmp_path / "project_active_mode_route"
    project_dir.mkdir(parents=True, exist_ok=True)
    session = manager.create(1, "codex", str(project_dir))
    session.modes.active_mode = "analyst"

    async def _fake_run_prompt(self, prompt: str, *args, **kwargs):
        _ = prompt, args, kwargs
        return "ok:routed"

    session.run_prompt = types.MethodType(_fake_run_prompt, session)

    cli_used, out = await run_prompt_routed_meta(
        session,
        cfg,
        "default",
        "Собери контекст по репозиторию.",
        chat_id=1,
    )

    assert cli_used == "claude"
    assert out == "ok:routed"

    artifact_store = RunArtifactStore(cfg)
    cli_run = artifact_store.latest_run(session=session, mode_id="cli")
    analyst_run = artifact_store.latest_run(session=session, mode_id="analyst")

    assert cli_run is not None
    assert analyst_run is None

    state = json.loads(Path(cli_run.state_path).read_text(encoding="utf-8"))
    assert state["mode_id"] == "cli"
    assert state["source"] == "cli_routing"
