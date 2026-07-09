"""Runtime and utility logic extracted from BotApp."""

from __future__ import annotations

import copy
import dataclasses
import logging
import os
from datetime import date
from types import SimpleNamespace
from typing import Any, Dict, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app.config_runtime.adapter import adapt_validated_settings
from app.config_runtime.loader import load_validated_settings
from app.security import SecurityFacade


class AppRuntimeService:
    EVENT_RELOADED = "runtime.config.reloaded"
    EVENT_RELOAD_FAILED = "runtime.config.reload_failed"
    RESTART_REQUIRED_FIELDS = (
        "telegram.token",
        "telegram.connection_pool_size",
        "telegram.connect_timeout_sec",
        "telegram.read_timeout_sec",
        "telegram.write_timeout_sec",
        "telegram.pool_timeout_sec",
        "telegram.polling_timeout_sec",
        "telegram.poll_interval_sec",
        "miniapp.enabled",
        "miniapp.bind_host",
        "miniapp.bind_port",
        "miniapp.base_path",
        "miniapp.max_edit_file_size_kb",
        "defaults.memory_events_enabled",
        "defaults.memory_native_cli_hooks_enabled",
        "defaults.memory_outcomes_enabled",
        "defaults.memory_dreaming_enabled",
        "defaults.memory_events_retention_days",
        "defaults.memory_events_max_payload_chars",
        "defaults.memory_events_redaction_enabled",
        "defaults.memory_dreaming_batch_size",
        "defaults.run_artifacts_enabled",
        "defaults.run_artifacts_retention_days",
        "defaults.run_doctor_enabled",
        "defaults.run_boundary_validation_enabled",
        "defaults.run_metrics_enabled",
        "defaults.skill_discovery_mode",
        "defaults.skill_install_policy",
        "defaults.skill_registry_paths",
        "defaults.skill_allowlisted_sources",
        "defaults.gemini_oauth_client_secret",
        "mcp.enabled",
        "mcp.host",
        "mcp.port",
        "webhooks.enabled",
        "webhooks.path",
        "webhooks.public_base_url",
        "webhooks.request_timeout_sec",
        "webhooks.max_payload_bytes",
    )
    RELOADABLE_DEFAULTS_FIELDS = (
        "cli_json_stream_archive_enabled",
        "assistant_preview_enabled",
        "pending_input_confirmation_enabled",
        "default_execution_backend",
        "openai_api_key",
        "zai_api_key",
        "github_token",
        "tavily_api_key",
        "jina_api_key",
        "default_language",
    )
    RELOADABLE_FIELDS = (
        "telegram.whitelist_chat_ids",
        "telegram.admlist_chat_ids",
        "telegram.user_workdirs",
        "telegram.user_modes",
        "telegram.user_languages",
        "miniapp.public_url",
        "miniapp.enable_delete",
        "mcp.token",
        "webhooks.secret_token",
    )

    def __init__(self, bot_app):
        self.bot_app = bot_app

    @staticmethod
    def _miniapp_daily_cache_buster() -> str:
        return date.today().strftime("%Y%m%d")

    def build_miniapp_webapp_url(self) -> Optional[str]:
        cfg = self.bot_app.config
        raw_path = str(getattr(cfg.miniapp, "base_path", "/cli-proxy") or "/cli-proxy").strip()
        if not raw_path.startswith("/"):
            raw_path = "/" + raw_path
        base_path = raw_path.rstrip("/") or "/cli-proxy"
        public_raw = str(getattr(cfg.miniapp, "public_url", "") or "").strip()
        if not public_raw:
            return None
        parsed = urlsplit(public_raw)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return None
        path = (parsed.path or "").strip()
        if not path or path == "/":
            path = base_path
        elif not path.startswith("/"):
            path = "/" + path
        path = "/" if path == "/" else (path.rstrip("/") + "/")
        query_items = [
            (key, value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if key != "v"
        ]
        query_items.append(("v", self._miniapp_daily_cache_buster()))
        return urlunsplit((parsed.scheme, parsed.netloc, path, urlencode(query_items), ""))

    async def reload_runtime_config(self) -> Dict[str, Any]:
        logger = logging.getLogger("miniapp")
        previous = self.bot_app.config
        try:
            fresh = adapt_validated_settings(load_validated_settings(previous.path), path=previous.path)
        except (SystemExit, Exception):
            logger.exception("runtime config reload failed while validating; previous config kept")
            result = {
                "status": "error",
                "applied": [],
                "restart_required": [],
                "warnings": ["Config validation failed. Previous config kept."],
            }
            await self._publish_system_event(self.EVENT_RELOAD_FAILED, previous.path, result)
            return result

        restart_required: list[str] = []
        applied: list[str] = []

        def _changed(a: Any, b: Any) -> bool:
            return a != b

        def _resolve_dotted(source: Any, dotted_path: str) -> Any:
            current = source
            for part in dotted_path.split("."):
                current = getattr(current, part)
            return current

        def _set_dotted(target: Any, dotted_path: str, value: Any) -> None:
            current = target
            parts = dotted_path.split(".")
            for part in parts[:-1]:
                current = getattr(current, part)
            setattr(current, parts[-1], copy.deepcopy(value))

        for dotted_path in self.RESTART_REQUIRED_FIELDS:
            if _changed(_resolve_dotted(previous, dotted_path), _resolve_dotted(fresh, dotted_path)):
                restart_required.append(dotted_path)
        for defaults_key in self.RELOADABLE_DEFAULTS_FIELDS:
            dotted_path = f"defaults.{defaults_key}"
            if _changed(_resolve_dotted(previous, dotted_path), _resolve_dotted(fresh, dotted_path)):
                applied.append(dotted_path)
        for dotted_path in self.RELOADABLE_FIELDS:
            if _changed(_resolve_dotted(previous, dotted_path), _resolve_dotted(fresh, dotted_path)):
                applied.append(dotted_path)

        tools_changed = _changed(previous.tools, fresh.tools)
        presets_changed = _changed(previous.presets, fresh.presets)
        security_changed = _changed(previous.security, fresh.security)
        thread_mode_changed = _changed(previous.thread_mode, fresh.thread_mode)
        scheduler_changed = _changed(previous.scheduler, fresh.scheduler)
        mcp_clients_changed = _changed(previous.mcp_clients, fresh.mcp_clients)
        lint_evolution_changed = _changed(previous.lint_evolution, fresh.lint_evolution)

        runtime_config = self._deepcopy_config_fields(previous)
        runtime_config.path = fresh.path
        for defaults_key in self.RELOADABLE_DEFAULTS_FIELDS:
            dotted_path = f"defaults.{defaults_key}"
            _set_dotted(runtime_config, dotted_path, _resolve_dotted(fresh, dotted_path))
        for dotted_path in self.RELOADABLE_FIELDS:
            _set_dotted(runtime_config, dotted_path, _resolve_dotted(fresh, dotted_path))
        if tools_changed:
            runtime_config.tools = copy.deepcopy(fresh.tools)
        if presets_changed:
            runtime_config.presets = copy.deepcopy(fresh.presets)
        if security_changed:
            runtime_config.security = copy.deepcopy(fresh.security)
        if lint_evolution_changed:
            runtime_config.lint_evolution = copy.deepcopy(fresh.lint_evolution)

        self.bot_app.config = runtime_config
        self.bot_app.manager.config = runtime_config
        self.bot_app.git.config = runtime_config
        self.bot_app.mcp.config = runtime_config
        if hasattr(self.bot_app, "is_admin") and hasattr(self.bot_app, "is_user"):
            self.bot_app.security = SecurityFacade.from_app_config(
                runtime_config,
                is_admin_fn=lambda chat_id: bool(self.bot_app.is_admin(int(chat_id))),
                is_user_fn=lambda chat_id: bool(self.bot_app.is_user(int(chat_id))),
                system_event_bus=getattr(self.bot_app, "system_event_bus", None),
            )
            if security_changed:
                applied.append("security")
        if lint_evolution_changed:
            from app.services.lint_evolution_runtime import make_session_hook

            router = getattr(self.bot_app, "mode_input_router", None)
            if router is not None and hasattr(router, "lint_evolution_hook"):
                router.lint_evolution_hook = make_session_hook(runtime_config.lint_evolution)
            applied.append("lint_evolution")
        for runtime in (getattr(self.bot_app, "iter_mode_runtimes", lambda: [])() or []):
            if runtime and hasattr(runtime, "set_config"):
                runtime.set_config(runtime_config)
        self.bot_app.session_ui.config = runtime_config

        if thread_mode_changed:
            restart_required.append("thread_mode")
        if scheduler_changed:
            restart_required.append("scheduler")
        if mcp_clients_changed:
            restart_required.append("mcp_clients")
        if tools_changed:
            applied.append("tools.*")
        if presets_changed:
            applied.append("presets")

        new_tool_keys = set((runtime_config.tools or {}).keys())
        for _chat_id, sessions in self.bot_app.manager.sessions_by_chat.items():
            for session in sessions.values():
                old_active_cli = self._session_active_cli(session)
                old_tool = (getattr(previous, "tools", None) or {}).get(old_active_cli) or getattr(session, "tool", None)
                old_backend = self._selected_execution_backend(previous, old_active_cli, old_tool)
                new_active_cli = old_active_cli if old_active_cli in new_tool_keys else (sorted(new_tool_keys)[0] if new_tool_keys else "")
                new_tool = (runtime_config.tools or {}).get(new_active_cli)
                new_backend = self._selected_execution_backend(runtime_config, new_active_cli, new_tool)
                if old_backend == "tmux" and (old_active_cli != new_active_cli or new_backend != "tmux"):
                    blockers = self._tmux_reload_blockers(session)
                    if blockers:
                        restart_required.append(f"session.{session.id}.tmux_backend")
                    else:
                        try:
                            await self._close_tmux_with_session_view(session, previous, old_active_cli, old_tool)
                            applied.append(f"session.{session.id}.tmux_closed")
                        except Exception:
                            logger.exception("runtime config reload failed to close old tmux session session_id=%s", session.id)
                            restart_required.append(f"session.{session.id}.tmux_backend")
                session.config = runtime_config
                active_cli = str(
                    getattr(getattr(session, "cli", None), "active_cli", "")
                    or getattr(session, "active_cli", "")
                    or ""
                ).strip()
                if active_cli in runtime_config.tools:
                    session.tool = runtime_config.tools[active_cli]
                elif new_tool_keys:
                    # Active CLI was removed, but there are other tools - switch to the first available one
                    fallback = sorted(new_tool_keys)[0]
                    try:
                        session.set_active_cli(fallback)
                        applied.append(f"session.{session.id}.active_cli->{fallback}")
                    except Exception:
                        # If set_active_cli fails (e.g., tool validation), fall back to restart_required
                        restart_required.append(f"tools.{active_cli}.removed")
                else:
                    # Active CLI was removed and there are no other tools available
                    restart_required.append(f"tools.{active_cli}.removed")
        warnings: list[str] = []
        if restart_required:
            warnings.append("Some changes require process restart.")
        status = "success_with_warnings" if warnings else "success"
        logger.info(
            "runtime config reload finished",
            extra={
                "chat_id": 0,
                "user_id": 0,
                "action": "reload",
                "path": previous.path,
                "status": status,
                "error": "; ".join(warnings),
            },
        )
        result = {
            "status": status,
            "applied": sorted(set(applied)),
            "restart_required": sorted(set(restart_required)),
            "warnings": warnings,
        }
        await self._publish_system_event(self.EVENT_RELOADED, previous.path, result)
        return result

    @staticmethod
    def _session_active_cli(session: Any) -> str:
        return str(
            getattr(getattr(session, "cli", None), "active_cli", "")
            or getattr(session, "active_cli", "")
            or getattr(getattr(session, "tool", None), "name", "")
            or ""
        ).strip()

    @staticmethod
    def _selected_execution_backend(config: Any, active_cli: str, tool: Any) -> str:
        if not active_cli or tool is None:
            return ""
        from session import get_session_execution_backend

        probe = SimpleNamespace(config=config, tool=tool, cli=SimpleNamespace(active_cli=active_cli))
        return str(get_session_execution_backend(probe, active_cli) or "").strip().lower()

    @staticmethod
    def _tmux_reload_blockers(session: Any) -> list[str]:
        blockers: list[str] = []
        if bool(getattr(session, "busy", False)):
            blockers.append("busy")
        run_lock = getattr(session, "run_lock", None)
        if run_lock is not None and hasattr(run_lock, "locked") and run_lock.locked():
            blockers.append("run_lock")
        if len(getattr(session, "queue", None) or []) > 0:
            blockers.append("queue")
        active_backend = str(getattr(session, "_active_execution_backend", "") or "").strip().lower()
        if active_backend == "tmux" and "active_execution_backend" not in blockers:
            blockers.append("active_execution_backend")
        return blockers

    @staticmethod
    async def _close_tmux_with_session_view(session: Any, config: Any, active_cli: str, tool: Any) -> None:
        from app.services.cli_backends import TmuxExecutionBackend

        cli_state = getattr(session, "cli", None)
        original_config = getattr(session, "config", None)
        original_tool = getattr(session, "tool", None)
        original_active_cli = getattr(session, "active_cli", None)
        original_cli_active = getattr(cli_state, "active_cli", None) if cli_state is not None else None
        try:
            session.config = config
            session.tool = tool
            if hasattr(session, "active_cli"):
                session.active_cli = active_cli
            if cli_state is not None:
                cli_state.active_cli = active_cli
            await TmuxExecutionBackend().close(session)
        finally:
            session.config = original_config
            session.tool = original_tool
            if hasattr(session, "active_cli"):
                session.active_cli = original_active_cli
            if cli_state is not None:
                cli_state.active_cli = original_cli_active

    async def _publish_system_event(self, event: str, path: str, result: Dict[str, Any]) -> None:
        bus = getattr(self.bot_app, "system_event_bus", None)
        if bus is None or not hasattr(bus, "publish"):
            return
        try:
            await bus.publish(
                event,
                {
                    "path": str(path),
                    "status": str(result.get("status", "") or ""),
                    "applied": list(result.get("applied", []) or []),
                    "restart_required": list(result.get("restart_required", []) or []),
                    "warnings": list(result.get("warnings", []) or []),
                },
            )
        except Exception:
            logging.getLogger(__name__).exception("runtime config reload event publish failed event=%s", event)

    @staticmethod
    def _deepcopy_config_fields(config: Any) -> Any:
        if not dataclasses.is_dataclass(config):
            return copy.deepcopy(config)
        values = {
            field.name: copy.deepcopy(getattr(config, field.name))
            for field in dataclasses.fields(config)
        }
        return type(config)(**values)

    @staticmethod
    def list_dir_entries(base: str) -> list[dict]:
        entries: list[dict] = []
        try:
            for name in os.listdir(base):
                path = os.path.join(base, name)
                try:
                    is_dir = os.path.isdir(path)
                except Exception:
                    logging.getLogger(__name__).exception("list_dir_entries: os.path.isdir failed for path=%s", path)
                    continue
                entries.append({"name": name, "path": path, "is_dir": is_dir})
        except Exception:
            logging.getLogger(__name__).exception("list_dir_entries: os.listdir failed for base=%s", base)
            return []
        entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
        return entries

    def preset_commands(self) -> Dict[str, str]:
        if self.bot_app.config.presets:
            return {p.name: p.prompt for p in self.bot_app.config.presets}
        return {
            "tests": "Запусти тесты и дай краткий отчёт.",
            "lint": "Запусти линтер/форматтер и дай краткий отчёт.",
            "build": "Запусти сборку и дай краткий отчёт.",
            "refactor": "Сделай небольшой рефакторинг по месту и объясни изменения.",
        }

    @staticmethod
    def guess_clone_path(url: str, base: str) -> Optional[str]:
        u = str(url or "").strip()
        if not u:
            return None
        path = u
        if u.startswith("git@") and ":" in u:
            path = u.split(":", 1)[1]
        elif "://" in u:
            path = u.split("://", 1)[1]
            if "/" in path:
                path = path.split("/", 1)[1]
        name = path.rstrip("/").split("/")[-1]
        if name.endswith(".git"):
            name = name[:-4]
        if not name:
            return None
        return os.path.join(base, name)
