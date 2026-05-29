from __future__ import annotations

from app.bootstrap import build_application as real_build_application
from bot import BotApp
from config import AppConfig, DefaultsConfig, MCPConfig, MiniAppConfig, TelegramConfig, ToolConfig


class _DummyToolRegistry:
    pass


def _build_config(tmp_path, *, intent: str) -> AppConfig:
    workdir = tmp_path / f"workdir_{intent}"
    runtime = tmp_path / f"runtime_{intent}"
    logs = tmp_path / f"logs_{intent}"
    workdir.mkdir(parents=True, exist_ok=True)
    runtime.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)

    return AppConfig(
        telegram=TelegramConfig(token="token", whitelist_chat_ids=[1], admlist_chat_ids=[1]),
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
            log_path=str(logs / "bot.log"),
            run_artifacts_retention_days=11 if intent == "bootstrap_use" else 30,
            skill_discovery_mode="auto" if intent == "bootstrap_use" else "suggest",
            skill_install_policy="admin_approve" if intent == "bootstrap_use" else "manual",
            skill_registry_paths=[".cli-proxy/skills", f".cli-proxy/{intent}-skills"],
            skill_allowlisted_sources=["local:global-registry", "registry:npx-skills"],
        ),
        mcp=MCPConfig(enabled=False),
        mcp_clients=[],
        presets=[],
        path=str(tmp_path / f"config_{intent}.yaml"),
        miniapp=MiniAppConfig(),
    )


def test_botapp_uses_build_application_dependencies(tmp_path, monkeypatch) -> None:
    cfg = _build_config(tmp_path, intent="bootstrap_use")
    sentinel_registry = _DummyToolRegistry()
    captured = {}

    def _spy_build_application(
        config: AppConfig,
        *,
        tool_registry_factory=None,
        run_mode_pipeline_fn=None,
        bot_app_provider=None,
    ):
        captured["config"] = config
        captured["bot_app_provider"] = bot_app_provider
        container = real_build_application(
            config,
            tool_registry_factory=lambda _cfg: sentinel_registry,
            run_mode_pipeline_fn=run_mode_pipeline_fn,
            bot_app_provider=bot_app_provider,
        )
        captured["container"] = container
        return container

    monkeypatch.setattr("bot.build_application", _spy_build_application)

    app = BotApp(cfg)
    try:
        assert captured.get("config") is cfg
        container = captured["container"]
        assert app.manager is container.session_manager
        assert app.mode_registry is container.mode_registry
        assert app.mode_loader is container.mode_loader
        assert app.mode_registry_service is container.mode_registry_service
        assert app.mode_tasks is container.mode_tasks
        assert app.mode_session_control is container.session_control
        assert app.mode_pipeline is container.mode_pipeline
        assert app.mode_run_artifacts is container.run_artifacts
        assert app.mode_run_observability is container.run_observability
        assert app.mode_run_doctor is container.run_doctor
        assert app.mode_run_boundary_validation is container.run_boundary_validation
        assert app.mode_skill_runtime is container.skill_runtime
        assert app.security is container.security
        assert app.shared_http_ingress is container.shared_http_ingress
        assert app.system_event_bus is container.system_event_bus
        assert app.scheduler_service is container.scheduler_service
        assert app.mode_launch_adapter is container.mode_launch_adapter_service
        assert app.mode_run_operations is container.run_operations_service
        assert app.remote_control_service is container.remote_control_service
        assert app.notification_queue_service is container.notification_queue_service
        assert app.cli_limits_service is container.cli_limits_service
        assert app.project_registry is container.project_registry
        assert app.session_thread_repository is container.session_thread_repository
        assert app.session_thread_manager is container.session_thread_manager
        assert app.advanced_orchestrator_service is container.advanced_orchestrator_service
        assert app.artifact_intent_service is container.artifact_intent_service
        assert captured["bot_app_provider"]() is app
        assert app._tool_registry is sentinel_registry
        assert container.mode_dependencies.session_manager is app.manager
        assert container.mode_dependencies.registry is app.mode_registry_service
        assert container.mode_dependencies.pipeline is app.mode_pipeline
        assert container.mode_dependencies.run_artifacts is app.mode_run_artifacts
        assert container.mode_dependencies.skill_runtime is app.mode_skill_runtime
    finally:
        app.shutdown_html_process_pool()


def test_botapp_injects_mode_dependencies_into_plugins(tmp_path) -> None:
    cfg = _build_config(tmp_path, intent="mode_deps")
    app = BotApp(cfg)
    try:
        mode_ids = app.mode_registry.list_ids()
        assert mode_ids
        for mode_id in mode_ids:
            plugin = app.mode_registry.get(mode_id)
            assert plugin is not None
            deps = getattr(plugin, "mode_dependencies", None)
            assert deps is not None
            assert deps.session_manager is app.manager
            assert deps.registry is app.mode_registry_service
            assert deps.pipeline is app.mode_pipeline
            assert deps.tasks is app.mode_tasks
            assert deps.dialogs is app.mode_dialogs
            assert deps.session_control is app.mode_session_control
            assert callable(deps.messaging_factory)
            assert getattr(deps.messaging_factory, "__self__", None) is app
            assert getattr(deps.messaging_factory, "__func__", None) is getattr(app._mode_messaging_factory, "__func__", None)
            assert deps.agent_runtime is app.mode_agent_runtime
            assert deps.dirs_flow is app.mode_dirs_flow
            assert deps.manager_pending is app.mode_manager_resume_pending
            assert deps.agent_pending is app.mode_agent_project_pending_by_chat
            assert callable(deps.runtime_by_capability)
            assert getattr(deps.runtime_by_capability, "__self__", None) is app
            assert getattr(deps.runtime_by_capability, "__func__", None) is getattr(app.get_runtime_by_capability, "__func__", None)
            assert deps.tooling is app.mode_tooling
            assert deps.run_artifacts is app.mode_run_artifacts
            assert deps.run_observability is app.mode_run_observability
            assert deps.run_doctor is app.mode_run_doctor
            assert deps.run_boundary_validation is app.mode_run_boundary_validation
            assert deps.skill_runtime is app.mode_skill_runtime
            assert deps.run_artifacts.retention_window_days() == 30
            assert deps.skill_runtime.registry_path_list() == [".cli-proxy/skills", ".cli-proxy/mode_deps-skills"]
            assert deps.skill_runtime.allows_source("registry:npx-skills") is True
    finally:
        app.shutdown_html_process_pool()


def test_botapp_sequential_runs_with_different_intent_do_not_leak_state(tmp_path) -> None:
    cfg_a = _build_config(tmp_path, intent="intent_a")
    cfg_b = _build_config(tmp_path, intent="intent_b")

    app_a = BotApp(cfg_a)
    app_b = None
    try:
        created_a = app_a.manager.create(chat_id=1, tool_name="dummy", workdir=cfg_a.defaults.workdir)
        assert created_a.id == "s1"
        # Replaced deprecated get_single_session_for_chat with sessions_for_chat check
        assert list(app_a.manager.sessions_for_chat(1).values()) == [created_a]

        app_b = BotApp(cfg_b)
        # Replaced deprecated get_single_session_for_chat with sessions_for_chat check
        assert app_b.manager.sessions_for_chat(1) == {}
        assert app_b.manager.sessions_for_chat(1) == {}
        assert app_a.mode_run_artifacts is not app_b.mode_run_artifacts
        assert app_a.mode_skill_runtime is not app_b.mode_skill_runtime
        assert app_a.mode_skill_runtime.registry_path_list() != app_b.mode_skill_runtime.registry_path_list()

        created_b = app_b.manager.create(chat_id=1, tool_name="dummy", workdir=cfg_b.defaults.workdir)
        assert created_b.id == "s1"
        assert len(app_a.manager.sessions_for_chat(1)) == 1
        assert len(app_b.manager.sessions_for_chat(1)) == 1
    finally:
        app_a.shutdown_html_process_pool()
        if app_b is not None:
            app_b.shutdown_html_process_pool()
