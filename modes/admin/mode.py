from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import time
from typing import Any, Awaitable, Callable, Dict, Mapping, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from app.mode_dependencies import ModeDependencies
from app.services.admin_config_service import AdminConfigService
from app.services.path_normalization import normalize_optional_state_path
from app.services.run_artifact_store import RunArtifactHandle
from app.services.telegram_ui_scope import TelegramUiKey
from modes.sdk import BaseMode, CallbackModel, MessageModel, ToolResult
from modes.sdk.run_artifacts_mixin import RunArtifactsMixin
from modes.sdk.services.callback_data import build_mode_action_callback_data
from modes.sdk.session_busy import is_session_busy
from session import session_runtime_uid

from .action_specs import (
    build_local_command_spec as _action_build_local_spec,
    build_ssh_command_spec as _action_build_ssh_spec,
    resolve_exec_action_payload as _action_resolve_payload,
)
from .allowlist import is_action_allowlisted, is_valid_action_id
from .chat_service import AdminChatService
from .runbook_facade import RunbookFacade
from .config_store import AdminConfigStore
from .executor import AdminExecutionContext, AdminExecutor, AdminExecutorError
from .facade import AdminAutonomyService
from .runner_service import AdminModeRunnerService
from .scanner import AdminEnvironmentScanner
from .schemas import AdminStatusPayloadSchema, validate_admin_payload
from .state_store import AdminStateStore
from .transports import (
    LocalCommandSpec,
    LocalSubprocessTransport,
    LocalTransportError,
    SSHCommandSpec,
    SSHSubprocessTransport,
    SSHTransportError,
)
from .ui import (
    build_admin_actions_screen,
    build_admin_approvals_screen,
    build_admin_autonomy_drifts_screen,
    build_admin_autonomy_status_screen,
    build_admin_baseline_screen,
    build_admin_error_text,
    build_admin_incidents_screen,
    build_admin_memory_screen,
    build_admin_menu_text,
    build_admin_rescan_report_screen,
    build_admin_runs_screen,
    build_admin_server_detail_screen,
    build_admin_servers_screen,
    build_admin_skills_screen,
    build_admin_status_text,
    merge_menu_with_note,
)

_UNSET = object()
_ADMIN_RUN_RESUME_GUARD_SESSION_ATTR = "_admin_run_resume_guard"


def _to_float_or_default(value: Any, default: float) -> float:
    try:
        if value is None or value == "":
            return float(default)
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _format_autopilot_exec_summary(
    *,
    intent: Dict[str, Any],
    exec_result: Dict[str, Any],
) -> str:
    itype = str(intent.get("type") or "")
    if itype == "propose_plan":
        total = exec_result.get("total_steps") or 0
        completed = exec_result.get("completed_steps") or 0
        header = f"✅ Autopilot выполнил plan ({completed}/{total} шагов)"
        if exec_result.get("stopped_early"):
            header = f"⚠ Autopilot остановил plan на шаге {completed}/{total}"
        chunks = [header]
        if exec_result.get("runbook_saved"):
            rb_id = str(exec_result.get("runbook_id") or "")
            chunks.append(f"Runbook сохранён: {rb_id}")
        return "\n".join(chunks)
    target_kind = str(exec_result.get("target_kind") or "?")
    action_id = str(intent.get("action_id") or "")
    argv_raw = intent.get("argv") if isinstance(intent.get("argv"), list) else None
    argv_text = " ".join(str(a) for a in (argv_raw or []))
    label = action_id or argv_text or itype
    header = f"✅ Autopilot выполнил `{label}` ({target_kind})"
    chunks = [header]
    exit_code = exec_result.get("exit_code")
    if exit_code is not None:
        chunks.append(f"Exit code: {exit_code}")
    stdout = str(exec_result.get("stdout") or "").strip()
    if stdout:
        snippet = stdout if len(stdout) <= 500 else stdout[:500] + "…"
        chunks.append(f"STDOUT:\n{snippet}")
    stderr = str(exec_result.get("stderr") or "").strip()
    if stderr:
        snippet = stderr if len(stderr) <= 500 else stderr[:500] + "…"
        chunks.append(f"STDERR:\n{snippet}")
    return "\n\n".join(chunks)


def _format_chat_result_for_telegram(result: Dict[str, Any]) -> str:
    chunks: list[str] = []
    target_kind = str(result.get("target_kind") or "")
    if target_kind == "plan":
        total = result.get("total_steps") or 0
        completed = result.get("completed_steps") or 0
        if result.get("ok"):
            chunks.append(f"Plan выполнен ({completed}/{total} шагов)")
        elif result.get("stopped_early"):
            chunks.append(f"Plan остановлен на шаге {completed}/{total}")
        else:
            chunks.append(f"Plan завершён с ошибками ({completed}/{total} шагов)")
        steps = result.get("steps")
        if isinstance(steps, list):
            for step in steps[:5]:
                if not isinstance(step, Mapping):
                    continue
                idx = step.get("step_index") or "?"
                argv_label = " ".join(str(a) for a in list(step.get("argv") or []))
                label = str(step.get("action_id") or argv_label or "-")
                status = "ok" if step.get("ok") else "failed"
                line = f"{idx}. {label}: {status}"
                if step.get("exit_code") is not None:
                    line += f" (exit_code={step.get('exit_code')})"
                chunks.append(line)
                stdout = str(step.get("stdout") or "").strip()
                if stdout:
                    snippet = stdout if len(stdout) <= 500 else stdout[:500] + "..."
                    chunks.append(f"STDOUT:\n{snippet}")
                stderr = str(step.get("stderr") or "").strip()
                if stderr:
                    snippet = stderr if len(stderr) <= 500 else stderr[:500] + "..."
                    chunks.append(f"STDERR:\n{snippet}")
        if result.get("runbook_saved"):
            chunks.append(f"Runbook сохранён: {result.get('runbook_id') or ''}")
        return "\n\n".join(chunks)
    action_id = str(result.get("action_id") or "")
    if action_id:
        label = "SSH action" if target_kind == "ssh" else "Local action"
        chunks.append(f"{label}: {action_id}")
    if target_kind == "ssh":
        host = str(result.get("host") or "")
        user = str(result.get("user") or "")
        port = str(result.get("port") or "22")
        target_line = f"{user + '@' if user else ''}{host}:{port}" if host else ""
        if target_line:
            chunks.append(f"Target: {target_line}")
    chunks.append(f"Exit code: {result.get('exit_code')}")
    duration = result.get("duration_ms")
    if duration:
        chunks.append(f"Duration: {duration}ms")
    if result.get("timed_out"):
        chunks.append("Timed out: yes")
    stdout = str(result.get("stdout") or "").strip()
    if stdout:
        chunks.append(f"STDOUT:\n{stdout}")
    stderr = str(result.get("stderr") or "").strip()
    if stderr:
        chunks.append(f"STDERR:\n{stderr}")
    return "\n\n".join(chunks) or "(no output)"


class AdminMode(BaseMode, RunArtifactsMixin):
    mode_id = "admin"
    display_name = "🛡 Admin"
    description = "Базовый режим администрирования для текущей сессии."
    _RUN_HANDLE_SESSION_ATTR = "_admin_run_handle"
    _EXEC_TARGETS = {"local", "ssh"}
    _EXEC_CONTROL_COMMANDS = {"check", "run"}
    _STATE_CONTROL_COMMANDS = {"incidents", "actions", "dry-run", "ack", "mute", "unmute", "approvals", "skills", "rescan"}
    _RUNNER_TASK_NAME = "run_admin_pipeline_loop"
    _ENVIRONMENT_SCAN_TASK_NAME = "scan_admin_environment"
    _AUTONOMY_TASK_NAME = "run_admin_autonomy_loop"
    _RUNNER_SLEEP_STEP_SEC = 0.25
    _RUNNER_DEFAULT_INTERVAL_SEC = 30.0
    _AUTONOMY_DEFAULT_INTERVAL_SEC = 60.0
    _PIPELINE_STATUS_ALLOWED = frozenset({"disabled", "initializing", "idle", "running", "completed", "failed"})
    _STEP_STATUS_ALLOWED = frozenset({"idle", "running", "completed", "failed", "skipped"})
    _COMPONENT_STATUS_ALLOWED = frozenset({"disabled", "idle", "ready", "running", "completed", "failed", "skipped"})

    def __init__(self, dependencies: Optional[ModeDependencies] = None) -> None:
        super().__init__(dependencies)
        self._log = logging.getLogger(__name__)
        self._local_transport = LocalSubprocessTransport()
        self._ssh_transport = SSHSubprocessTransport()
        self._executor = AdminExecutor()
        self._chat_service = AdminChatService(
            local_transport=self._local_transport,
            ssh_transport=self._ssh_transport,
            logger=self._log,
        )
        self._runbook = RunbookFacade(self)

    def build_runtime(self, config: Any) -> Any:
        return AdminModeRunnerService(config)

    async def on_enable(self, ctx: Dict[str, Any]) -> Optional[ToolResult]:
        session = ctx.get("session")
        bot_app = ctx.get("bot_app")
        if not session or not bot_app:
            return None
        context = ctx.get("context")
        raw_chat_id = (
            ctx.get("chat_id")
            or (ctx.get("dest") or {}).get("chat_id")
            or getattr(session, "chat_id", None)
        )
        try:
            chat_id = int(raw_chat_id) if raw_chat_id not in (None, "") else None
        except Exception:
            chat_id = None
        if chat_id is None:
            return None
        try:
            ms = self._messaging(bot_app=bot_app, context=context)
        except Exception:
            ms = None
        try:
            await self._activate_admin_runtime(
                bot_app=bot_app,
                session=session,
                chat_id=chat_id,
                user_id=ctx.get("user_id"),
                context=context,
                ms=ms,
            )
        except Exception:
            self._log.exception(
                "admin on_enable runtime activation failed session_id=%s", getattr(session, "id", "")
            )
            return ToolResult.fail("admin_on_enable_failed")
        await self._activate_mode(
            session=session,
            bot_app=bot_app,
            cli_work_type=None,
            executor_profile=None,
        )
        return None

    async def on_disable(self, ctx: Dict[str, Any]) -> Optional[ToolResult]:
        session = ctx.get("session")
        bot_app = ctx.get("bot_app")
        if not session or not bot_app:
            return None
        raw_chat_id = (
            ctx.get("chat_id")
            or (ctx.get("dest") or {}).get("chat_id")
            or getattr(session, "chat_id", None)
        )
        try:
            chat_id = int(raw_chat_id) if raw_chat_id not in (None, "") else None
        except Exception:
            chat_id = None
        try:
            await self._deactivate_admin_runtime(
                bot_app=bot_app,
                session=session,
                chat_id=chat_id,
                user_id=ctx.get("user_id"),
            )
        except Exception:
            self._log.exception(
                "admin on_disable runtime deactivation failed session_id=%s",
                getattr(session, "id", ""),
            )
            return ToolResult.fail("admin_on_disable_failed")
        await self._deactivate_mode(
            session=session,
            bot_app=bot_app,
            cancel_tasks=False,
            timeout_s=0.2,
        )
        return None

    async def _activate_admin_runtime(
        self,
        *,
        bot_app: Any,
        session: Any,
        chat_id: int,
        user_id: Any = None,
        context: Any = None,
        ms: Any = None,
    ) -> None:
        """Idempotent bootstrap: schema, config, session state, initial scan, runner."""
        store = self._get_state_store(bot_app=bot_app)
        store.ensure_schema()
        config_store = AdminConfigStore(str(getattr(session, "workdir", "") or ""))
        config_store.ensure_config()
        pinned_runtime = self._resolve_session_pinned_runtime(session)
        config_store.update_runtime(
            pinned_cli=pinned_runtime.get("pinned_cli") or None,
            pinned_executor_profile=pinned_runtime.get("pinned_executor_profile"),
            initialized_at=time.time(),
            scan_error=None,
        )
        cfg_payload = config_store.load_config(validate=True)
        admin_cfg = cfg_payload.get("admin", {}) if isinstance(cfg_payload, dict) else {}
        monitor_cfg = admin_cfg.get("monitor", {}) if isinstance(admin_cfg, dict) else {}
        watch_enabled = bool(monitor_cfg.get("enabled", True)) if isinstance(monitor_cfg, dict) else True
        self._upsert_session_status(
            bot_app=bot_app,
            session=session,
            chat_id=str(chat_id),
            enabled=True,
            watch_enabled=watch_enabled,
            updated_by=int(user_id) if isinstance(user_id, int) else None,
            last_error=None,
        )
        self._set_session_admin_enabled(session=session, enabled=True)
        self._set_runtime_status(
            session=session,
            pipeline_status="initializing",
            monitor_status="idle",
            analyzer_status="idle",
            executor_status="idle",
            notifier_status="idle",
            analyzer_message="Waiting for the initial environment scan.",
            executor_message="No execution requested yet.",
        )
        await self._ensure_initial_environment_scan_started(
            bot_app=bot_app,
            session=session,
            config_payload=cfg_payload,
            chat_id=chat_id,
            context=context,
        )
        refreshed_cfg = self._load_admin_config(bot_app=bot_app, session=session, effective=False)
        if not self._needs_initial_environment_scan(refreshed_cfg):
            messaging = ms if ms is not None else self._messaging(bot_app=bot_app, context=context)
            await self._ensure_runner_started(
                bot_app=bot_app,
                session=session,
                chat_id=chat_id,
                messaging=messaging,
                context=context,
            )
        await self._ensure_autonomy_runner_started(bot_app=bot_app, session=session)

    async def _deactivate_admin_runtime(
        self,
        *,
        bot_app: Any,
        session: Any,
        chat_id: Optional[int] = None,
        user_id: Any = None,
    ) -> None:
        """Cancel mode tasks and mark admin disabled in shared state."""
        await self._cancel_admin_tasks(session=session)
        effective_chat_id = chat_id if chat_id is not None else int(getattr(session, "chat_id", 0) or 0)
        self._upsert_session_status(
            bot_app=bot_app,
            session=session,
            chat_id=str(effective_chat_id),
            enabled=False,
            updated_by=int(user_id) if isinstance(user_id, int) else None,
        )
        self._set_session_admin_enabled(session=session, enabled=False)
        self._set_runtime_status(
            session=session,
            pipeline_status="disabled",
            monitor_status="idle",
            analyzer_status="idle",
            executor_status="idle",
            notifier_status="idle",
            analyzer_message="Admin is disabled for this session.",
            executor_message="Admin is disabled for this session.",
        )

    @staticmethod
    def _resolve_session_cli_mode(session: Any) -> str:
        cli_value = (
            getattr(session, "cli_mode", None)
            or getattr(getattr(session, "cli", None), "active_cli", None)
            or getattr(session, "active_cli", None)
        )
        return str(cli_value or "").strip()

    @staticmethod
    def _resolve_session_pinned_runtime(session: Any) -> Dict[str, Any]:
        pinned_cli_name = AdminMode._resolve_session_cli_mode(session)
        pinned_cli = {"name": pinned_cli_name} if pinned_cli_name else {}
        cli_state = getattr(session, "cli", None)
        for key in ("transport", "target", "host", "user", "port", "key_path", "options"):
            value = getattr(cli_state, key, None)
            if value is None or value == "" or value == () or value == []:
                value = getattr(session, key, None)
            if value is None or value == "" or value == () or value == []:
                continue
            pinned_cli[key] = list(value) if isinstance(value, tuple) else value
        executor_profile = str(getattr(session, "executor_profile", "") or "").strip() or None
        return {
            "pinned_cli": pinned_cli,
            "pinned_executor_profile": executor_profile,
        }

    async def handle_input(self, message: MessageModel, ctx: Dict[str, Any]) -> ToolResult:
        bot_app = ctx.get("bot_app")
        session = ctx.get("session")
        context = ctx.get("context")
        if not bot_app or not session:
            return ToolResult.fail("missing_context")

        ms = self._messaging(bot_app=bot_app, context=context)
        parsed = self._parse_input_action(
            str(getattr(message, "text", "") or ""),
            session=session,
        )
        action = str(parsed.get("action") or "").strip().lower()
        message_text = str(parsed.get("message") or "").strip()
        payload = parsed.get("payload") if isinstance(parsed, dict) else None

        handlers = {
            "help": lambda: self._input_show_menu(
                bot_app=bot_app,
                session=session,
                chat_id=str(message.chat_id),
                ms=ms,
            ),
            "status": lambda: self._input_show_status(
                bot_app=bot_app,
                session=session,
                chat_id=str(message.chat_id),
                ms=ms,
            ),
            "enable_mode": lambda: self._input_enable_mode(
                bot_app=bot_app,
                session=session,
                chat_id=str(message.chat_id),
                user_id=getattr(message, "user_id", None),
                ms=ms,
                context=context,
            ),
            "disable_mode": lambda: self._input_disable_mode(
                bot_app=bot_app,
                session=session,
                chat_id=str(message.chat_id),
                user_id=getattr(message, "user_id", None),
                ms=ms,
            ),
            "error": lambda: self._input_show_error(
                chat_id=str(message.chat_id),
                ms=ms,
                text=message_text,
            ),
        }
        handlers["exec_local"] = lambda: self._input_run_local(
            bot_app=bot_app,
            session=session,
            chat_id=str(message.chat_id),
            ms=ms,
            action_id=message_text,
        )
        handlers["exec_ssh"] = lambda: self._input_run_ssh(
            bot_app=bot_app,
            session=session,
            chat_id=str(message.chat_id),
            ms=ms,
            action_id=message_text,
        )
        handlers["exec_action"] = lambda: self._input_execute_action(
            bot_app=bot_app,
            session=session,
            chat_id=str(message.chat_id),
            user_id=getattr(message, "user_id", None),
            ms=ms,
            payload=payload if isinstance(payload, dict) else {},
        )
        handlers["list_incidents"] = lambda: self._input_list_incidents(
            bot_app=bot_app,
            session=session,
            chat_id=str(message.chat_id),
            ms=ms,
            payload=payload if isinstance(payload, dict) else {},
        )
        handlers["list_actions"] = lambda: self._input_list_actions(
            bot_app=bot_app,
            session=session,
            chat_id=str(message.chat_id),
            ms=ms,
            payload=payload if isinstance(payload, dict) else {},
        )
        handlers["ack_alert"] = lambda: self._input_ack_alert(
            bot_app=bot_app,
            session=session,
            chat_id=str(message.chat_id),
            user_id=getattr(message, "user_id", None),
            ms=ms,
            incident_id=message_text,
        )
        handlers["set_dry_run"] = lambda: self._input_set_dry_run(
            bot_app=bot_app,
            session=session,
            chat_id=str(message.chat_id),
            user_id=getattr(message, "user_id", None),
            ms=ms,
            payload=payload if isinstance(payload, dict) else {},
        )
        handlers["mute_alerts"] = lambda: self._input_mute_alerts(
            bot_app=bot_app,
            session=session,
            chat_id=str(message.chat_id),
            ms=ms,
            payload=payload if isinstance(payload, dict) else {},
        )
        handlers["unmute_alerts"] = lambda: self._input_unmute_alerts(
            bot_app=bot_app,
            session=session,
            chat_id=str(message.chat_id),
            ms=ms,
        )
        handlers["list_approvals"] = lambda: self._input_list_approvals(
            bot_app=bot_app,
            session=session,
            chat_id=str(message.chat_id),
            ms=ms,
        )
        handlers["rescan_environment"] = lambda: self._input_rescan_environment(
            bot_app=bot_app,
            session=session,
            chat_id=str(message.chat_id),
            user_id=getattr(message, "user_id", None),
            ms=ms,
            context=context,
        )
        handlers["revoke_approval"] = lambda: self._input_revoke_approval(
            bot_app=bot_app,
            session=session,
            chat_id=str(message.chat_id),
            ms=ms,
            override_id=message_text,
        )
        handlers["clear_approvals"] = lambda: self._input_clear_approvals(
            bot_app=bot_app,
            session=session,
            chat_id=str(message.chat_id),
            ms=ms,
        )
        handlers["list_skill_installs"] = lambda: self._input_list_skill_installs(
            bot_app=bot_app,
            session=session,
            chat_id=str(message.chat_id),
            ms=ms,
        )
        handlers["approve_skill_install"] = lambda: self._input_approve_skill_install(
            bot_app=bot_app,
            session=session,
            chat_id=str(message.chat_id),
            ms=ms,
            approval_id=message_text,
        )
        handlers["reject_skill_install"] = lambda: self._input_reject_skill_install(
            bot_app=bot_app,
            session=session,
            chat_id=str(message.chat_id),
            ms=ms,
            approval_id=message_text,
        )
        handlers["chat"] = lambda: self._input_handle_chat(
            bot_app=bot_app,
            session=session,
            chat_id=str(message.chat_id),
            user_id=getattr(message, "user_id", None),
            ms=ms,
            user_text=message_text,
        )
        dispatched = await self._dispatch_input_action(action=action, handlers=handlers)
        if dispatched is not None:
            return dispatched

        # Default path for free-text input in active mode.
        return await self._input_show_status(
            bot_app=bot_app,
            session=session,
            chat_id=str(message.chat_id),
            ms=ms,
        )

    def _parse_input_action(self, text: str, *, session: Any) -> Dict[str, Any]:
        _ = session
        raw = str(text or "").strip()
        if not raw:
            return {"action": "help"}
        if not raw.startswith("/"):
            return {"action": "chat", "message": raw}

        parts = [p for p in raw.split() if p]
        if not parts:
            return {"action": "help"}
        command = parts[0].lstrip("/").split("@", 1)[0].strip().lower()
        if command != "admin":
            return {"action": "chat", "message": raw}
        if len(parts) == 1:
            return {"action": "help"}

        subcommand = str(parts[1] or "").strip().lower()
        if subcommand in {"help", "status"}:
            if len(parts) != 2:
                return {"action": "error", "message": f"Формат: /admin {subcommand}."}
            return {"action": subcommand}
        if subcommand in {"enable", "on"}:
            if len(parts) != 2:
                return {"action": "error", "message": "Формат: /admin enable."}
            return {"action": "enable_mode"}
        if subcommand in {"disable", "off"}:
            if len(parts) != 2:
                return {"action": "error", "message": "Формат: /admin disable."}
            return {"action": "disable_mode"}
        if subcommand in self._EXEC_CONTROL_COMMANDS:
            return self._parse_executor_action(parts=parts, session=session, command=subcommand)
        if subcommand in self._STATE_CONTROL_COMMANDS:
            return self._parse_state_control_action(parts=parts, command=subcommand)
        return {
            "action": "error",
            "message": (
                "Неизвестная подкоманда `/admin ...`. "
                "Используйте `/admin help`."
            ),
        }

    @staticmethod
    def _resolve_session_id(*, session: Any) -> str:
        session_id = str(getattr(session, "id", "") or "").strip()
        if not session_id:
            raise RuntimeError("session_id is empty")
        return session_id

    def _upsert_session_status(
        self,
        *,
        bot_app: Any,
        session: Any,
        chat_id: int,
        enabled: Optional[bool] = None,
        dry_run: Optional[bool] = None,
        watch_enabled: Optional[bool] = None,
        updated_by: Any = _UNSET,
        last_error: Any = _UNSET,
    ) -> Dict[str, Any]:
        store = self._get_state_store(bot_app=bot_app)
        session_id = self._resolve_session_id(session=session)
        return store.upsert_session_state(
            session_id,
            chat_id=str(chat_id),
            enabled=enabled,
            dry_run=dry_run,
            watch_enabled=watch_enabled,
            updated_by=updated_by,
            last_error=last_error,
        )

    def _parse_state_control_action(self, *, parts: list[str], command: str) -> Dict[str, Any]:
        cmd = str(command or "").strip().lower()
        if cmd == "incidents":
            if len(parts) > 3:
                return {"action": "error", "message": "Формат: /admin incidents [N]."}
            limit = 20
            if len(parts) == 3:
                try:
                    limit = int(str(parts[2] or "").strip())
                except Exception:
                    return {"action": "error", "message": "N должен быть целым числом > 0."}
            if limit <= 0:
                return {"action": "error", "message": "N должен быть > 0."}
            return {"action": "list_incidents", "payload": {"limit": min(limit, 200)}}
        if cmd == "actions":
            if len(parts) > 3:
                return {"action": "error", "message": "Формат: /admin actions [N]."}
            limit = 20
            if len(parts) == 3:
                try:
                    limit = int(str(parts[2] or "").strip())
                except Exception:
                    return {"action": "error", "message": "N должен быть целым числом > 0."}
            if limit <= 0:
                return {"action": "error", "message": "N должен быть > 0."}
            return {"action": "list_actions", "payload": {"limit": min(limit, 200)}}
        if cmd == "dry-run":
            if len(parts) != 3:
                return {"action": "error", "message": "Формат: /admin dry-run <on|off>."}
            mode = str(parts[2] or "").strip().lower()
            if mode not in {"on", "off"}:
                return {"action": "error", "message": "Поддерживаются значения: on | off."}
            return {"action": "set_dry_run", "payload": {"dry_run": mode == "on"}}
        if cmd == "ack":
            if len(parts) != 3:
                return {"action": "error", "message": "Формат: /admin ack <incident_id>."}
            incident_id = str(parts[2] or "").strip()
            if not is_valid_action_id(incident_id):
                return {
                    "action": "error",
                    "message": "Некорректный incident_id. Допустимы буквы, цифры, символы `._:-`.",
                }
            return {"action": "ack_alert", "message": incident_id}
        if cmd == "mute":
            if len(parts) > 3:
                return {"action": "error", "message": "Формат: /admin mute [minutes]."}
            minutes = 60.0
            if len(parts) == 3:
                try:
                    minutes = float(str(parts[2] or "").strip())
                except Exception:
                    return {"action": "error", "message": "Параметр minutes должен быть числом > 0."}
            if minutes <= 0:
                return {"action": "error", "message": "Параметр minutes должен быть > 0."}
            return {"action": "mute_alerts", "payload": {"minutes": minutes}}
        if cmd == "unmute":
            if len(parts) != 2:
                return {"action": "error", "message": "Формат: /admin unmute."}
            return {"action": "unmute_alerts"}
        if cmd == "approvals":
            if len(parts) < 3:
                return {
                    "action": "error",
                    "message": "Формат: /admin approvals <list|revoke|clear> [override_id].",
                }
            op = str(parts[2] or "").strip().lower()
            if op == "list":
                if len(parts) != 3:
                    return {"action": "error", "message": "Формат: /admin approvals list."}
                return {"action": "list_approvals"}
            if op == "revoke":
                if len(parts) != 4:
                    return {"action": "error", "message": "Формат: /admin approvals revoke <override_id>."}
                override_id = str(parts[3] or "").strip()
                if not is_valid_action_id(override_id):
                    return {
                        "action": "error",
                        "message": "Некорректный override_id. Допустимы буквы, цифры, символы `._:-`.",
                    }
                return {"action": "revoke_approval", "message": override_id}
            if op == "clear":
                if len(parts) != 3:
                    return {"action": "error", "message": "Формат: /admin approvals clear."}
                return {"action": "clear_approvals"}
            return {
                "action": "error",
                "message": "Поддерживаются: /admin approvals list|revoke|clear.",
            }
        if cmd == "skills":
            if len(parts) < 3:
                return {
                    "action": "error",
                    "message": "Формат: /admin skills <list|approve|reject> [approval_id].",
                }
            op = str(parts[2] or "").strip().lower()
            if op == "list":
                if len(parts) != 3:
                    return {"action": "error", "message": "Формат: /admin skills list."}
                return {"action": "list_skill_installs"}
            if op in {"approve", "reject"}:
                if len(parts) != 4:
                    return {
                        "action": "error",
                        "message": f"Формат: /admin skills {op} <approval_id>.",
                    }
                approval_id = str(parts[3] or "").strip()
                if not is_valid_action_id(approval_id):
                    return {
                        "action": "error",
                        "message": "Некорректный approval_id. Допустимы буквы, цифры, символы `._:-`.",
                    }
                return {
                    "action": "approve_skill_install" if op == "approve" else "reject_skill_install",
                    "message": approval_id,
                }
            return {
                "action": "error",
                "message": "Поддерживаются: /admin skills list|approve|reject.",
            }
        if cmd == "rescan":
            if len(parts) != 2:
                return {"action": "error", "message": "Формат: /admin rescan."}
            return {"action": "rescan_environment"}
        return {"action": "status"}

    def _parse_executor_action(self, *, parts: list[str], session: Any, command: str) -> Dict[str, Any]:
        _ = session
        if command == "check":
            if len(parts) != 3:
                return {"action": "error", "message": "Формат: /admin check <server_id>."}
            server_id = str(parts[2] or "").strip()
            if not is_valid_action_id(server_id):
                return {"action": "error", "message": "Некорректный server_id."}
            return {
                "action": "exec_action",
                "payload": {
                    "command": "check",
                    "server_id": server_id,
                    "check_only": True,
                },
            }

        if len(parts) != 4:
            return {"action": "error", "message": "Формат: /admin run <action_id> <server_id>."}
        action_id = str(parts[2] or "").strip()
        server_id = str(parts[3] or "").strip()
        if not is_valid_action_id(action_id):
            return {"action": "error", "message": "Некорректный action_id."}
        if not is_valid_action_id(server_id):
            return {"action": "error", "message": "Некорректный server_id."}
        return {
            "action": "exec_action",
            "payload": {
                "command": "run",
                "action_id": action_id,
                "server_id": server_id,
                "check_only": False,
            },
        }

    def _load_admin_config(
        self,
        *,
        session: Any,
        effective: bool = True,
        bot_app: Any = None,
    ) -> Dict[str, Any]:
        if bot_app is not None:
            service = getattr(bot_app, "admin_config_service", None)
            if service is None:
                service = AdminConfigService(bot_app)
            loader = getattr(service, "load_config", None)
            if callable(loader):
                try:
                    return loader(session_runtime_uid(session), effective=effective)
                except Exception:
                    self._log.exception(
                        "admin config service load failed for parser session_id=%s",
                        getattr(session, "id", ""),
                    )
                    return {}
        try:
            store = AdminConfigStore(str(getattr(session, "workdir", "") or ""))
            if effective:
                return store.load_effective_config()
            return store.load_config()
        except Exception:
            self._log.exception("admin config load failed for parser session_id=%s", getattr(session, "id", ""))
            return {}

    @staticmethod
    def _resolve_state_path(*, bot_app: Any) -> str:
        cfg = getattr(bot_app, "config", None)
        defaults = getattr(cfg, "defaults", None) if cfg is not None else None
        try:
            state_path = normalize_optional_state_path(getattr(defaults, "state_path", None)) or ""
        except TypeError:
            state_path = ""
        if not state_path:
            raise RuntimeError("state_path is empty")
        return state_path

    def _get_state_store(self, *, bot_app: Any) -> AdminStateStore:
        return AdminStateStore(self._resolve_state_path(bot_app=bot_app))

    @staticmethod
    def _set_session_admin_enabled(*, session: Any, enabled: bool) -> None:
        try:
            setattr(session, "admin_enabled", bool(enabled))
        except Exception:
            logging.getLogger(__name__).exception(
                "admin session flag update failed session_id=%s enabled=%s",
                getattr(session, "id", ""),
                bool(enabled),
            )

    def _get_admin_session_state(self, *, bot_app: Any, session: Any, chat_id: int) -> Dict[str, Any]:
        try:
            state = self._get_state_store(bot_app=bot_app).get_session_state(
                str(getattr(session, "id", "") or ""),
                chat_id=str(chat_id),
            )
            return dict(state or {})
        except Exception:
            self._log.exception(
                "admin state read failed session_id=%s chat_id=%s",
                getattr(session, "id", ""),
                chat_id,
            )
            return {}

    def _is_admin_enabled(self, *, bot_app: Any, session: Any, chat_id: int) -> bool:
        state = self._get_admin_session_state(bot_app=bot_app, session=session, chat_id=chat_id)
        enabled = bool(state.get("enabled", False))
        self._set_session_admin_enabled(session=session, enabled=enabled)
        return enabled

    def _is_admin_watch_enabled(self, *, bot_app: Any, session: Any, chat_id: int) -> bool:
        state = self._get_admin_session_state(bot_app=bot_app, session=session, chat_id=chat_id)
        return bool(state.get("enabled", False) and state.get("watch_enabled", False))

    async def _cancel_admin_tasks(self, *, session: Any) -> None:
        svc = self.get_service("session_control")
        if svc is None:
            return
        await svc.cancel_mode(session_id=session_runtime_uid(session), mode_id=self.mode_id, timeout_s=0.2)

    @staticmethod
    def _is_session_selected_for_chat(*, bot_app: Any, session: Any) -> bool:
        scope = getattr(session, "conversation_scope", None)
        if scope is None:
            return True
        resolver = getattr(bot_app, "resolve_telegram_scope_session", None)
        if not callable(resolver):
            return True
        try:
            current = resolver(
                reply_chat_id=int(getattr(scope, "chat_id", 0) or 0),
                message_thread_id=getattr(scope, "message_thread_id", None),
                owner_chat_id=int(getattr(session, "chat_id", 0) or 0),
            )
        except Exception:
            return True
        if current is None:
            return False
        active_id = str(getattr(current, "id", "") or "").strip()
        session_id = str(getattr(session, "id", "") or "").strip()
        return bool(active_id and session_id and active_id == session_id)

    @staticmethod
    def _resolve_runner_interval_sec(*, config_payload: Dict[str, Any]) -> float:
        admin_cfg = config_payload.get("admin", {}) if isinstance(config_payload, dict) else {}
        monitor_cfg = admin_cfg.get("monitor", {}) if isinstance(admin_cfg, dict) else {}
        raw_interval = (
            monitor_cfg.get("interval_sec")
            if isinstance(monitor_cfg, dict)
            else AdminMode._RUNNER_DEFAULT_INTERVAL_SEC
        )
        try:
            interval_sec = float(raw_interval)
        except Exception:
            interval_sec = AdminMode._RUNNER_DEFAULT_INTERVAL_SEC
        if interval_sec <= 0:
            interval_sec = AdminMode._RUNNER_DEFAULT_INTERVAL_SEC
        return interval_sec

    def _resolve_runner_service(self) -> Optional[Any]:
        runtime_getter = self._optional_runtime_getter()
        if runtime_getter is None:
            return None
        runtime = runtime_getter("run_admin_pipeline")
        if runtime is None:
            return None
        required_api = ("run_pipeline_once", "ensure_notifier", "is_pipeline_ready")
        if all(callable(getattr(runtime, name, None)) for name in required_api):
            return runtime
        return None

    def _prepare_run_artifacts(
        self,
        *,
        session: Any,
        source_text: str,
        phase: str,
        operation_payload: Dict[str, Any],
        mode_context: Optional[Dict[str, Any]] = None,
    ) -> tuple[Optional[RunArtifactHandle], Dict[str, Any]]:
        self._clear_active_run_handle(session)
        if not self._is_run_artifacts_enabled():
            return None, {}
        artifact_store = self._artifact_store()
        if artifact_store is None:
            return None, {}

        resume_guard: Dict[str, Any] = {}
        latest = self._latest_mode_run(session)
        if latest is not None:
            latest_state = artifact_store.load_state(latest)
            if not self._is_terminal_run_status(latest_state.get("status")):
                report = self._diagnose_resume_boundary(latest)
                if report is not None:
                    resume_guard = {
                        "previous_run_id": latest.run_id,
                        "report": report.to_dict(),
                    }
                artifact_store.mark_finished(
                    latest,
                    status="failed",
                    phase=str(latest_state.get("phase") or phase or "complete"),
                )
                resume_guard["previous_run_repaired"] = True

        merged_mode_context = {
            "operation_payload": dict(operation_payload or {}),
            "run_scope": "admin_runtime",
            "resume_guard": dict(resume_guard or {}),
        }
        merged_mode_context.update(dict(mode_context or {}))
        run = artifact_store.start_run(
            session=session,
            mode_id=self.mode_id,
            phase=str(phase or "complete"),
            source_prompt_hash=self._prompt_hash(source_text),
            mode_context=merged_mode_context,
        )
        self._set_active_run_handle(session, run)
        try:
            setattr(session, _ADMIN_RUN_RESUME_GUARD_SESSION_ATTR, dict(resume_guard or {}))
        except Exception:
            self._log.exception("admin run artifacts: failed to set resume guard session attr")
        return run, resume_guard

    def _clear_active_run_handle(self, session: Any) -> None:
        # Override: дополнительно очищает _ADMIN_RUN_RESUME_GUARD_SESSION_ATTR
        super()._clear_active_run_handle(session)
        if hasattr(session, _ADMIN_RUN_RESUME_GUARD_SESSION_ATTR):
            setattr(session, _ADMIN_RUN_RESUME_GUARD_SESSION_ATTR, {})

    @staticmethod
    def _operation_plan_payload(
        *,
        operation_payload: Dict[str, Any],
        phase: str,
    ) -> Dict[str, Any]:
        operation_kind = str((operation_payload or {}).get("kind") or "admin_operation").strip() or "admin_operation"
        return {
            "plan_kind": "admin_operation",
            "task_family": operation_kind,
            "units": [
                {
                    "id": f"admin:{operation_kind}",
                    "operation_payload": dict(operation_payload or {}),
                }
            ],
            "boundary_map": [{"phase": str(phase or "complete")}],
            "validation_contracts": ["snapshot_fidelity" if operation_kind == "watch_loop" else "native_transport"],
        }

    @staticmethod
    def _checkpoint_payload(
        *,
        phase: str,
        status: str,
        operation_payload: Dict[str, Any],
        target_transport: str = "",
        snapshot_id: str = "",
        message: str = "",
    ) -> Dict[str, Any]:
        operation_kind = str((operation_payload or {}).get("kind") or "admin_operation").strip() or "admin_operation"
        payload: Dict[str, Any] = {
            "phase": str(phase or "complete"),
            "unit_id": f"admin:{operation_kind}:{str(phase or 'complete').strip() or 'complete'}",
            "status": str(status or "started"),
            "operation_kind": operation_kind,
            "operation_payload": dict(operation_payload or {}),
        }
        if target_transport:
            payload["target_transport"] = str(target_transport)
        if snapshot_id:
            payload["snapshot_id"] = str(snapshot_id)
        if message:
            payload["message"] = str(message or "")[:500]
        return payload

    @classmethod
    def _iter_snapshot_entries(cls, snapshot: Any) -> list[Any]:
        if isinstance(snapshot, Mapping):
            raw_servers = snapshot.get("servers", []) or []
        else:
            raw_servers = getattr(snapshot, "servers", []) or []
        return list(raw_servers) if isinstance(raw_servers, (list, tuple)) else []

    @classmethod
    def _entry_field(cls, entry: Any, field: str) -> Any:
        if isinstance(entry, Mapping):
            return entry.get(field)
        return getattr(entry, field, None)

    @classmethod
    def _build_snapshot_entry_id(cls, entry: Any, index: int) -> str:
        server_id = str(cls._entry_field(entry, "server_id") or f"server-{index + 1}").strip() or f"server-{index + 1}"
        target = str(cls._entry_field(entry, "target") or "unknown").strip() or "unknown"
        action_id = str(cls._entry_field(entry, "action_id") or "action").strip() or "action"
        collected_at = cls._entry_field(entry, "collected_at_ts")
        try:
            collected_token = str(int(float(collected_at or 0.0) * 1000.0))
        except Exception:
            collected_token = str(index)
        raw_id = f"{server_id}:{target}:{action_id}:{collected_token}"
        if len(raw_id) <= 128:
            return raw_id
        digest = hashlib.sha256(raw_id.encode("utf-8")).hexdigest()[:16]
        return f"{raw_id[:111]}:{digest}"

    @classmethod
    def _resolve_snapshot_transport(cls, snapshot: Any, *, fallback: str = "") -> str:
        if fallback:
            return str(fallback)
        transports = {
            str(cls._entry_field(entry, "target") or "").strip().lower()
            for entry in cls._iter_snapshot_entries(snapshot)
            if str(cls._entry_field(entry, "target") or "").strip()
        }
        if len(transports) == 1:
            return next(iter(transports))
        if len(transports) > 1:
            return "mixed"
        return "logical"

    @classmethod
    def _build_snapshot_trace(cls, snapshot: Any, *, target_transport: str = "") -> Dict[str, Any]:
        summary = cls._summarize_snapshot(snapshot)
        snapshot_ids = [
            cls._build_snapshot_entry_id(entry, index)
            for index, entry in enumerate(cls._iter_snapshot_entries(snapshot))
        ]
        created_at = None
        if isinstance(snapshot, Mapping):
            created_at = snapshot.get("created_at_ts")
        else:
            created_at = getattr(snapshot, "created_at_ts", None)
        bundle_seed = "|".join(snapshot_ids) or str(created_at or "")
        snapshot_id = f"snapshot:{hashlib.sha256(bundle_seed.encode('utf-8')).hexdigest()[:16]}"
        return {
            "snapshot_id": snapshot_id,
            "snapshot_ids": snapshot_ids,
            "target_transport": cls._resolve_snapshot_transport(snapshot, fallback=target_transport),
            "last_monitor_snapshot": summary,
            "snapshot_fidelity": {
                "snapshot_id": snapshot_id,
                "snapshot_ids": list(snapshot_ids),
                "server_count": int(summary.get("server_count") or 0),
                "total_servers": int(summary.get("total_servers") or summary.get("server_count") or 0),
                "ok_servers": int(summary.get("ok_servers") or 0),
                "failed_servers": int(summary.get("failed_servers") or 0),
                "verified_post_analyze": True,
            },
        }

    @classmethod
    def _is_valid_monitor_server(cls, row: Any) -> bool:
        if not isinstance(row, Mapping):
            return False
        server_id = str(row.get("id") or row.get("server_id") or "").strip()
        target = str(row.get("target") or "").strip().lower()
        action_id = str(row.get("action_id") or "").strip()
        return bool(server_id and target in cls._EXEC_TARGETS and action_id)

    @classmethod
    def _has_monitor_servers(cls, config_payload: Dict[str, Any]) -> bool:
        admin_cfg = config_payload.get("admin", {}) if isinstance(config_payload, dict) else {}
        monitor_cfg = admin_cfg.get("monitor", {}) if isinstance(admin_cfg, dict) else {}
        servers = monitor_cfg.get("servers", ()) if isinstance(monitor_cfg, dict) else ()
        if isinstance(servers, (list, tuple)):
            return any(cls._is_valid_monitor_server(row) for row in servers)
        return False

    def _build_tool_ctx(
        self,
        *,
        bot_app: Any,
        session: Any,
        context: Any,
        chat_id: int,
    ) -> Dict[str, Any]:
        return {
            "cwd": getattr(session, "workdir", None),
            "state_root": getattr(session, "workdir", None),
            "session_id": getattr(session, "id", None),
            "chat_id": str(chat_id),
            "chat_type": "private",
            "bot": bot_app,
            "context": context,
            "session": session,
            "allowed_tools": ["ask_user"],
            "corr_id": f"admin:{getattr(session, 'id', 'unknown')}:approval",
        }

    def _build_runner_ask_user(
        self,
        *,
        bot_app: Any,
        session: Any,
        context: Any,
        chat_id: int,
    ) -> Optional[Callable[[str, list[str]], Awaitable[str]]]:
        tooling = self._optional_tooling()
        if tooling is None:
            return None
        tool_ctx = self._build_tool_ctx(
            bot_app=bot_app,
            session=session,
            context=context,
            chat_id=chat_id,
        )

        async def _ask_user(question: str, options: list[str]) -> str:
            raw = self._runtime_status_snapshot(session)
            self._set_runtime_status(
                session=session,
                pipeline_status=self._normalize_pipeline_status(raw.get("pipeline_status"), default="running"),
                monitor_status=self._normalize_step_status(raw.get("monitor_status"), default="completed"),
                analyzer_status=self._normalize_step_status(raw.get("analyzer_status"), default="completed"),
                executor_status=self._normalize_step_status(raw.get("executor_status"), default="idle"),
                notifier_status=self._normalize_step_status(raw.get("notifier_status"), default="idle"),
                analyzer_message=raw.get("analyzer_message"),
                executor_message=raw.get("executor_message"),
                notifier_message=raw.get("notifier_message"),
                extra={
                    "pending_ask_user": {
                        "pending": True,
                        "question": str(question or ""),
                        "options": list(options or []),
                    }
                },
            )
            try:
                selected = await tooling.ask_user(
                    question=str(question or ""),
                    options=list(options or []),
                    allow_custom=False,
                    system_options=False,
                    ctx=tool_ctx,
                )
            finally:
                raw_after = self._runtime_status_snapshot(session)
                self._set_runtime_status(
                    session=session,
                    pipeline_status=self._normalize_pipeline_status(raw_after.get("pipeline_status"), default="running"),
                    monitor_status=self._normalize_step_status(raw_after.get("monitor_status"), default="completed"),
                    analyzer_status=self._normalize_step_status(raw_after.get("analyzer_status"), default="completed"),
                    executor_status=self._normalize_step_status(raw_after.get("executor_status"), default="idle"),
                    notifier_status=self._normalize_step_status(raw_after.get("notifier_status"), default="idle"),
                    analyzer_message=raw_after.get("analyzer_message"),
                    executor_message=raw_after.get("executor_message"),
                    notifier_message=raw_after.get("notifier_message"),
                    extra={"pending_ask_user": {}},
                )
            return str(selected or "")

        return _ask_user

    @classmethod
    def _normalize_pipeline_status(cls, value: Any, *, default: str) -> str:
        text = str(value or "").strip().lower()
        if text in cls._PIPELINE_STATUS_ALLOWED:
            return text
        return str(default or "idle")

    @classmethod
    def _normalize_step_status(cls, value: Any, *, default: str) -> str:
        text = str(value or "").strip().lower()
        if text in cls._STEP_STATUS_ALLOWED:
            return text
        return str(default or "idle")

    @classmethod
    def _normalize_component_status(cls, value: Any, *, default: str) -> str:
        text = str(value or "").strip().lower()
        if text in cls._COMPONENT_STATUS_ALLOWED:
            return text
        return str(default or "idle")

    @staticmethod
    def _clean_runtime_message(value: Any, *, max_len: int = 240) -> str:
        text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
        if len(text) <= max_len:
            return text
        return text[: max_len - 3] + "..."

    @staticmethod
    def _runtime_status_snapshot(session: Any) -> Dict[str, Any]:
        raw = getattr(session, "admin_runtime_status", None)
        if isinstance(raw, dict):
            return dict(raw)
        return {}

    @classmethod
    def _set_runtime_status(
        cls,
        *,
        session: Any,
        pipeline_status: str,
        monitor_status: str,
        analyzer_status: str,
        executor_status: str,
        notifier_status: str = "idle",
        analyzer_message: str = "",
        executor_message: str = "",
        notifier_message: str = "",
        extra: Optional[Mapping[str, Any]] = None,
    ) -> None:
        payload = cls._runtime_status_snapshot(session)
        payload.update(
            {
                "pipeline_status": cls._normalize_pipeline_status(pipeline_status, default="idle"),
                "monitor_status": cls._normalize_component_status(monitor_status, default="idle"),
                "analyzer_status": cls._normalize_step_status(analyzer_status, default="idle"),
                "analyzer_message": cls._clean_runtime_message(analyzer_message),
                "executor_status": cls._normalize_step_status(executor_status, default="idle"),
                "executor_message": cls._clean_runtime_message(executor_message),
                "notifier_status": cls._normalize_component_status(notifier_status, default="idle"),
                "notifier_message": cls._clean_runtime_message(notifier_message),
                "status_updated_at": float(time.time()),
            }
        )
        if isinstance(extra, Mapping):
            payload.update(dict(extra))
        setattr(
            session,
            "admin_runtime_status",
            payload,
        )

    def _derive_runtime_status_payload(
        self,
        *,
        session: Any,
        active: bool,
        mode_tasks_running: bool,
    ) -> Dict[str, Any]:
        raw = self._runtime_status_snapshot(session)
        pipeline_default = "disabled" if not active else ("running" if mode_tasks_running else "idle")
        updated_at = raw.get("status_updated_at")
        try:
            updated_at_value = float(updated_at) if updated_at is not None else None
        except Exception:
            updated_at_value = None
        payload = {
            "pipeline_status": self._normalize_pipeline_status(raw.get("pipeline_status"), default=pipeline_default),
            "monitor_status": self._normalize_component_status(
                raw.get("monitor_status"),
                default="disabled" if not active else ("running" if mode_tasks_running else "ready"),
            ),
            "analyzer_status": self._normalize_step_status(raw.get("analyzer_status"), default="idle"),
            "analyzer_message": self._clean_runtime_message(raw.get("analyzer_message")),
            "executor_status": self._normalize_step_status(raw.get("executor_status"), default="idle"),
            "executor_message": self._clean_runtime_message(raw.get("executor_message")),
            "notifier_status": self._normalize_component_status(
                raw.get("notifier_status"),
                default="disabled" if not active else "ready",
            ),
            "notifier_message": self._clean_runtime_message(raw.get("notifier_message")),
        }
        if updated_at_value is not None:
            payload["status_updated_at"] = updated_at_value
        for key in (
            "pinned_cli",
            "pinned_executor_profile",
            "initialized_at",
            "last_scan_at",
            "scan_status",
            "scan_error",
            "last_monitor_snapshot",
            "last_analyzer_decision",
            "last_action_result",
            "pending_ask_user",
        ):
            value = raw.get(key)
            if isinstance(value, dict):
                payload[key] = dict(value)
            elif value not in {None, ""}:
                payload[key] = value
        return payload

    def build_status_payload(self, *, bot_app: Any, session: Any, chat_id: Any = None) -> Dict[str, Any]:
        return self._status_payload(bot_app=bot_app, session=session, chat_id=chat_id)

    @staticmethod
    def _summarize_snapshot(snapshot: Any) -> Dict[str, Any]:
        if snapshot is None:
            return {}
        if isinstance(snapshot, Mapping):
            return AdminMode._summarize_runtime_snapshot(snapshot)
        raw_servers = getattr(snapshot, "servers", ()) or ()
        servers: list[Dict[str, Any]] = []
        for item in list(raw_servers)[:5]:
            metrics = getattr(item, "metrics", {})
            servers.append(
                {
                    "server_id": str(getattr(item, "server_id", "") or ""),
                    "ok": bool(getattr(item, "ok", False)),
                    "error": str(getattr(item, "error", "") or ""),
                    "metrics": dict(metrics) if isinstance(metrics, Mapping) else {},
                }
            )
        return AdminMode._summarize_runtime_snapshot(
            {
                "created_at_ts": getattr(snapshot, "created_at_ts", None),
                "server_count": len(raw_servers),
                "total_servers": getattr(snapshot, "total_servers", None),
                "ok_servers": getattr(snapshot, "ok_servers", None),
                "failed_servers": getattr(snapshot, "failed_servers", None),
                "servers": servers,
            }
        )

    @staticmethod
    def _summarize_decision(decision: Mapping[str, Any]) -> Dict[str, Any]:
        if not isinstance(decision, Mapping):
            return {}
        summary: Dict[str, Any] = {}
        for key in ("diagnosis", "confidence", "action", "reason", "urgency", "secondary_cli_command"):
            value = decision.get(key)
            if value not in {None, ""}:
                summary[str(key)] = value
        return summary

    @staticmethod
    def _summarize_execution_result(result: Any) -> Dict[str, Any]:
        if result is None:
            return {}
        return {
            "success": bool(getattr(result, "success", False)),
            "text": str(getattr(result, "text", "") or ""),
            "returncode": getattr(result, "returncode", None),
            "logged_action_id": getattr(result, "logged_action_id", None),
        }

    @staticmethod
    def _summarize_runtime_snapshot(snapshot: Mapping[str, Any]) -> Dict[str, Any]:
        summary: Dict[str, Any] = {}
        created_at = snapshot.get("created_at_ts")
        if created_at not in {None, ""}:
            summary["created_at_ts"] = created_at
        servers = snapshot.get("servers", [])
        if isinstance(servers, list):
            raw_server_count = snapshot.get("server_count")
            try:
                server_count = int(raw_server_count)
            except (TypeError, ValueError):
                server_count = len(servers)
            summary["server_count"] = max(0, server_count)
            summary["servers"] = [
                {
                    "server_id": str(item.get("server_id") or ""),
                    "ok": bool(item.get("ok", False)),
                    "error": str(item.get("error") or ""),
                }
                for item in servers[:5]
                if isinstance(item, Mapping)
            ]
        for key in ("total_servers", "ok_servers", "failed_servers"):
            if key in snapshot:
                summary[key] = snapshot.get(key)
        return summary

    @staticmethod
    def _summarize_state_rows(rows: list[Dict[str, Any]], *, id_key: str) -> list[Dict[str, Any]]:
        out: list[Dict[str, Any]] = []
        for row in rows[:5]:
            if not isinstance(row, dict):
                continue
            payload = row.get("payload")
            out.append(
                {
                    id_key: str(row.get(id_key) or ""),
                    "created_at": row.get("created_at"),
                    "updated_at": row.get("updated_at"),
                    "payload": dict(payload) if isinstance(payload, Mapping) else {},
                }
            )
        return out

    @staticmethod
    def _resolve_pending_ask_user(bot_app: Any, *, session_id: str, chat_id: int, session: Any = None) -> Dict[str, Any]:
        pending_map = bot_app.ui_state.pending_questions
        active_by_chat = bot_app.ui_state.active_ask_question_by_chat
        runtime_pending = {}
        if session is not None:
            raw_status = getattr(session, "admin_runtime_status", None)
            if isinstance(raw_status, dict) and isinstance(raw_status.get("pending_ask_user"), dict):
                runtime_pending = dict(raw_status.get("pending_ask_user") or {})
        scope = getattr(session, "conversation_scope", None) if session is not None else None
        ui_key = TelegramUiKey.from_parts(chat_id, getattr(scope, "message_thread_id", None))
        matches: list[Dict[str, Any]] = []
        for question_id, meta in pending_map.items():
            if not isinstance(meta, dict):
                continue
            if str(meta.get("session_id") or "") != str(session_id or ""):
                continue
            if int(meta.get("chat_id") or 0) != int(chat_id):
                continue
            matches.append(
                {
                    "question_id": str(question_id or ""),
                    "allow_custom": bool(meta.get("allow_custom", False)),
                    "created_at": meta.get("created_at"),
                    "options": list(meta.get("options") or []),
                }
            )
        active_question_id = str(active_by_chat.get(ui_key) or "").strip()
        active_payload = next(
            (item for item in matches if str(item.get("question_id") or "") == active_question_id),
            None,
        )
        return {
            "count": len(matches) or (1 if runtime_pending.get("pending") else 0),
            "active": bool(active_payload) or bool(runtime_pending.get("pending")),
            "current": dict(active_payload or runtime_pending or {}),
        }

    async def _sleep_runner_interval(
        self,
        *,
        bot_app: Any,
        session: Any,
        chat_id: int,
        interval_sec: float,
    ) -> bool:
        deadline = time.monotonic() + max(0.0, float(interval_sec))
        while True:
            if not self._is_admin_watch_enabled(bot_app=bot_app, session=session, chat_id=str(chat_id)):
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return True
            await asyncio.sleep(min(self._RUNNER_SLEEP_STEP_SEC, remaining))

    async def _ensure_runner_started(
        self,
        *,
        bot_app: Any,
        session: Any,
        chat_id: int,
        messaging: Any,
        context: Any,
    ) -> None:
        try:
            running_tasks = set(self._mode_task_names(bot_app=bot_app, session=session))
        except Exception:
            self._log.exception("admin runner list mode tasks failed session_id=%s", getattr(session, "id", ""))
            return
        if not self._is_admin_watch_enabled(bot_app=bot_app, session=session, chat_id=str(chat_id)):
            return
        if self._RUNNER_TASK_NAME in running_tasks:
            return

        runner = self._resolve_runner_service()
        if runner is None:
            self._log.warning("admin runner runtime is unavailable; skip background start")
            return

        try:
            state_store = self._get_state_store(bot_app=bot_app)
            runner.ensure_notifier(state_store=state_store)
            ready = bool(runner.is_pipeline_ready())
        except Exception:
            self._log.exception("admin runner readiness check failed session_id=%s", getattr(session, "id", ""))
            return

        if not ready:
            self._log.warning("admin runner is not ready; background task is not started")
            return
        if isinstance(runner, AdminModeRunnerService):
            config_payload = self._load_admin_config(bot_app=bot_app, session=session)
            if not self._has_monitor_servers(config_payload):
                self._log.info(
                    "admin runner skipped because monitor.servers has no valid poll specs session_id=%s",
                    getattr(session, "id", ""),
                )
                self._set_runtime_status(
                    session=session,
                    pipeline_status="idle",
                    monitor_status="idle",
                    analyzer_status="idle",
                    executor_status="idle",
                    notifier_status="ready",
                    analyzer_message="No valid monitor servers configured.",
                    executor_message="No execution requested yet.",
                )
                return

        session_id = str(getattr(session, "id", "") or "").strip()
        session_workdir = str(getattr(session, "workdir", "") or "")
        state_store = self._get_state_store(bot_app=bot_app)
        ask_user = self._build_runner_ask_user(
            bot_app=bot_app,
            session=session,
            context=context,
            chat_id=chat_id,
        )

        async def _run_loop() -> None:
            while True:
                if not self._is_admin_watch_enabled(bot_app=bot_app, session=session, chat_id=str(chat_id)):
                    break
                config_payload = self._load_admin_config(bot_app=bot_app, session=session)
                interval_sec = self._resolve_runner_interval_sec(config_payload=config_payload)
                operation_payload = {
                    "kind": "watch_loop",
                    "chat_id": str(chat_id),
                    "interval_sec": float(interval_sec),
                    "session_id": session_id,
                }
                run: Optional[RunArtifactHandle] = None
                current_phase = "analyze"
                try:
                    run, _resume_guard = self._prepare_run_artifacts(
                        session=session,
                        source_text=f"/admin enable watch:{session_id}:{chat_id}",
                        phase="analyze",
                        operation_payload=operation_payload,
                        mode_context={"target_transport": "logical"},
                    )
                    self._save_run_plan(
                        run,
                        self._operation_plan_payload(operation_payload=operation_payload, phase="analyze"),
                    )
                    self._append_checkpoint(
                        run,
                        self._checkpoint_payload(
                            phase="analyze",
                            status="started",
                            operation_payload=operation_payload,
                            message="Admin watch loop iteration started.",
                        ),
                    )
                    self._append_run_event(
                        run,
                        {
                            "event_type": "admin_watch_iteration_start",
                            "operation_kind": "watch_loop",
                            "phase": "analyze",
                        },
                    )
                    self._set_runtime_status(
                        session=session,
                        pipeline_status="running",
                        monitor_status="running",
                        analyzer_status="running",
                        executor_status="idle",
                        notifier_status="idle",
                        analyzer_message="Analyzer inspects the latest monitor snapshot.",
                        executor_message="Waiting for analyzer decision.",
                    )
                    step_result = await runner.run_pipeline_once(
                        config_payload=config_payload,
                        session_workdir=session_workdir,
                        session_id=session_id,
                        chat_id=str(chat_id),
                        state_store=state_store,
                        messaging=messaging,
                        ask_user=ask_user,
                    )
                    if step_result is None:
                        raise RuntimeError("admin runner returned empty pipeline result")
                    decision = dict(getattr(step_result.monitor_analyzer, "decision", {}) or {})
                    execution_result = getattr(step_result.executor_notifier, "execution_result", None)
                    execution_meta = getattr(step_result, "executor_notifier", None)
                    action_name = str(decision.get("action") or "notify_admin").strip() or "notify_admin"
                    confidence = str(decision.get("confidence") or "-").strip() or "-"
                    diagnosis = str(decision.get("diagnosis") or decision.get("reason") or "").strip()
                    analyzer_summary = f"{diagnosis or action_name} ({confidence})"
                    executor_success = bool(getattr(execution_result, "success", False))
                    executor_summary = str(getattr(execution_result, "text", "") or "").strip()
                    if not executor_summary:
                        executor_summary = "Execution finished successfully." if executor_success else "Execution failed."
                    snapshot = getattr(step_result.monitor_analyzer, "snapshot", None)
                    action_notification = getattr(step_result.executor_notifier, "action_notification", None)
                    incident_notification = getattr(step_result.executor_notifier, "incident_notification", None)
                    snapshot_trace = self._build_snapshot_trace(
                        snapshot,
                        target_transport=str(getattr(execution_meta, "target_transport", "") or ""),
                    )
                    execution_context = {
                        "native_transport_execution": bool(
                            getattr(execution_meta, "native_transport_execution", False)
                        ),
                        "skill_selector_bypassed": bool(
                            getattr(execution_meta, "native_transport_execution", False)
                        ),
                        "skill_selector_bypass_reason": (
                            "native_admin_transport"
                            if bool(getattr(execution_meta, "native_transport_execution", False))
                            else ""
                        ),
                        "destructive_execution": bool(
                            getattr(execution_meta, "destructive_execution", False)
                        ),
                        "dry_run": bool(getattr(execution_meta, "dry_run", False)),
                        "check_only": bool(getattr(execution_meta, "check_only", False)),
                        "action_id": str(getattr(execution_meta, "action_id", "") or action_name),
                        "server_id": str(getattr(execution_meta, "server_id", "") or ""),
                        "command_source": "admin_watch_loop",
                    }
                    self._save_run_state(
                        run,
                        phase="analyze",
                        status="running",
                        mode_context={
                            **snapshot_trace,
                            "last_analyzer_decision": self._summarize_decision(decision),
                            "execution_context": execution_context,
                        },
                    )
                    self._append_checkpoint(
                        run,
                        self._checkpoint_payload(
                            phase="analyze",
                            status="ok",
                            operation_payload=operation_payload,
                            target_transport=str(snapshot_trace.get("target_transport") or ""),
                            snapshot_id=str(snapshot_trace.get("snapshot_id") or ""),
                            message=analyzer_summary,
                        ),
                    )
                    self._validate_run_boundary(run, phase="analyze")
                    current_phase = "complete"
                    self._set_runtime_status(
                        session=session,
                        pipeline_status="running",
                        monitor_status="completed",
                        analyzer_status="completed",
                        executor_status="completed" if executor_success else "failed",
                        notifier_status="completed" if action_notification or incident_notification else "idle",
                        analyzer_message=analyzer_summary,
                        executor_message=executor_summary,
                        notifier_message="Notifications sent." if action_notification or incident_notification else "",
                        extra={
                            "last_monitor_snapshot": self._summarize_snapshot(snapshot),
                            "last_analyzer_decision": self._summarize_decision(decision),
                            "last_action_result": self._summarize_execution_result(execution_result),
                        },
                    )
                    final_status = "completed" if executor_success else "failed"
                    self._save_run_state(
                        run,
                        phase="complete",
                        status=final_status,
                        mode_context={
                            **snapshot_trace,
                            "last_analyzer_decision": self._summarize_decision(decision),
                            "last_action_result": self._summarize_execution_result(execution_result),
                            "execution_context": execution_context,
                            "target_transport": str(snapshot_trace.get("target_transport") or ""),
                        },
                    )
                    self._append_checkpoint(
                        run,
                        self._checkpoint_payload(
                            phase="complete",
                            status="ok" if executor_success else "error",
                            operation_payload=operation_payload,
                            target_transport=str(snapshot_trace.get("target_transport") or ""),
                            snapshot_id=str(snapshot_trace.get("snapshot_id") or ""),
                            message=executor_summary,
                        ),
                    )
                    self._validate_run_boundary(run, phase="complete")
                    self._mark_run_finished(run, status=final_status, phase="complete")
                    self._append_run_event(
                        run,
                        {
                            "event_type": "admin_watch_iteration_complete",
                            "operation_kind": "watch_loop",
                            "phase": "complete",
                            "status": final_status,
                            "action": action_name,
                            "target_transport": str(snapshot_trace.get("target_transport") or ""),
                            "snapshot_id": str(snapshot_trace.get("snapshot_id") or ""),
                        },
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._save_run_state(
                        run,
                        phase=current_phase,
                        status="failed",
                        mode_context={
                            "runtime_error": str(exc or ""),
                            "execution_context": {"command_source": "admin_watch_loop"},
                        },
                    )
                    self._append_checkpoint(
                        run,
                        self._checkpoint_payload(
                            phase=current_phase,
                            status="error",
                            operation_payload=operation_payload,
                            message=str(exc or ""),
                        ),
                    )
                    self._append_run_event(
                        run,
                        {
                            "event_type": "admin_watch_iteration_failed",
                            "operation_kind": "watch_loop",
                            "phase": current_phase,
                            "status": "failed",
                            "error": str(exc or ""),
                        },
                    )
                    self._mark_run_finished(run, status="failed", phase=current_phase)
                    self._set_runtime_status(
                        session=session,
                        pipeline_status="failed",
                        monitor_status="failed",
                        analyzer_status="failed",
                        executor_status="skipped",
                        notifier_status="skipped",
                        analyzer_message="Pipeline iteration failed before a final decision.",
                        executor_message=str(exc),
                    )
                    self._log.exception("admin runner loop iteration failed session_id=%s", session_id)
                finally:
                    self._clear_active_run_handle(session)
                should_continue = await self._sleep_runner_interval(
                    bot_app=bot_app,
                    session=session,
                    chat_id=str(chat_id),
                    interval_sec=interval_sec,
                )
                if not should_continue:
                    break

        try:
            self._start_mode_task(
                bot_app=bot_app,
                session=session,
                coro=_run_loop(),
                name=self._RUNNER_TASK_NAME,
            )
        except Exception:
            self._log.exception("admin runner start failed session_id=%s", session_id)

    async def _ensure_autonomy_runner_started(
        self,
        *,
        bot_app: Any,
        session: Any,
    ) -> None:
        """
        Запускает фоновый autonomy tick loop (параллельно watch-loop'у).
        Gated на admin.autonomy.enabled=true. Идемпотентно.
        """
        workdir = str(getattr(session, "workdir", "") or "")
        if not workdir:
            return
        try:
            running_tasks = set(self._mode_task_names(bot_app=bot_app, session=session))
        except Exception:
            self._log.exception(
                "admin autonomy list mode tasks failed session_id=%s", getattr(session, "id", ""),
            )
            return
        if self._AUTONOMY_TASK_NAME in running_tasks:
            return
        try:
            service = AdminAutonomyService(workdir)
            policy = service.load_autonomy_policy()
        except Exception:
            self._log.exception(
                "admin autonomy policy load failed session_id=%s", getattr(session, "id", ""),
            )
            return
        if not policy.enabled:
            return
        interval_sec = self._resolve_autonomy_interval_sec(workdir)
        session_id = str(getattr(session, "id", "") or "").strip()

        async def _autonomy_loop() -> None:
            while True:
                try:
                    await service.run_autonomy_tick()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self._log.exception(
                        "admin autonomy tick failed session_id=%s", session_id,
                    )
                try:
                    await asyncio.sleep(interval_sec)
                except asyncio.CancelledError:
                    raise

        try:
            self._start_mode_task(
                bot_app=bot_app,
                session=session,
                coro=_autonomy_loop(),
                name=self._AUTONOMY_TASK_NAME,
            )
        except Exception:
            self._log.exception(
                "admin autonomy start failed session_id=%s", session_id,
            )

    def _resolve_autonomy_interval_sec(self, workdir: str) -> float:
        try:
            cfg = AdminConfigStore(workdir).load_effective_config()
        except Exception:
            return self._AUTONOMY_DEFAULT_INTERVAL_SEC
        admin_cfg = cfg.get("admin") if isinstance(cfg, Mapping) else {}
        autonomy_cfg = admin_cfg.get("autonomy") if isinstance(admin_cfg, Mapping) else {}
        raw = (autonomy_cfg or {}).get("interval_sec") if isinstance(autonomy_cfg, Mapping) else None
        try:
            interval = float(raw) if raw is not None else self._AUTONOMY_DEFAULT_INTERVAL_SEC
        except (TypeError, ValueError):
            interval = self._AUTONOMY_DEFAULT_INTERVAL_SEC
        return interval if interval > 0 else self._AUTONOMY_DEFAULT_INTERVAL_SEC

    @staticmethod
    def _runtime_cfg_from_admin_config(config_payload: Dict[str, Any]) -> Dict[str, Any]:
        admin_cfg = config_payload.get("admin", {}) if isinstance(config_payload, dict) else {}
        runtime_cfg = admin_cfg.get("runtime", {}) if isinstance(admin_cfg, dict) else {}
        return dict(runtime_cfg) if isinstance(runtime_cfg, dict) else {}

    @classmethod
    def _environment_scan_profile_from_config(cls, config_payload: Dict[str, Any]) -> Dict[str, Any]:
        runtime_cfg = cls._runtime_cfg_from_admin_config(config_payload)
        pinned_cli = runtime_cfg.get("pinned_cli", {})
        if pinned_cli is None:
            pinned_cli = {}
        if not isinstance(pinned_cli, dict):
            return {}
        profile = dict(pinned_cli)
        if cls._scan_profile_has_explicit_target(profile):
            return profile

        monitor_server = cls._single_monitor_scan_server(config_payload)
        if monitor_server is None:
            return profile
        target = str(monitor_server.get("target") or monitor_server.get("transport") or "").strip().lower()
        if target not in {"local", "ssh"}:
            return profile

        profile.setdefault("name", str(runtime_cfg.get("cli_name") or profile.get("name") or "").strip())
        profile["target"] = target
        profile["transport"] = target
        for key in ("host", "user", "port", "key_path", "password", "password_env", "options"):
            value = monitor_server.get(key)
            if value not in (None, ""):
                profile[key] = value
        return {key: value for key, value in profile.items() if value not in (None, "")}

    @staticmethod
    def _scan_profile_has_explicit_target(profile: Mapping[str, Any]) -> bool:
        target = str(profile.get("target") or profile.get("transport") or "").strip().lower()
        if target in {"local", "ssh"}:
            return True
        return bool(
            profile.get("host")
            and (
                profile.get("key_path")
                or profile.get("password")
                or profile.get("password_env")
            )
        )

    @staticmethod
    def _single_monitor_scan_server(config_payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        admin_cfg = config_payload.get("admin", {}) if isinstance(config_payload, dict) else {}
        monitor_cfg = admin_cfg.get("monitor", {}) if isinstance(admin_cfg, dict) else {}
        servers = monitor_cfg.get("servers", []) if isinstance(monitor_cfg, dict) else []
        if not isinstance(servers, list):
            return None
        candidates: list[Dict[str, Any]] = []
        for item in servers:
            if not isinstance(item, dict):
                continue
            target = str(item.get("target") or item.get("transport") or "").strip().lower()
            if target in {"local", "ssh"}:
                candidates.append(dict(item))
        if len(candidates) != 1:
            return None
        return candidates[0]

    @staticmethod
    def _needs_initial_environment_scan(config_payload: Dict[str, Any]) -> bool:
        runtime_cfg = AdminMode._runtime_cfg_from_admin_config(config_payload)
        if "last_scan_at" not in runtime_cfg:
            return True
        return runtime_cfg.get("last_scan_at") in {None, ""}

    def _build_environment_scanner(
        self,
        *,
        pinned_cli: Dict[str, Any],
        session_workdir: str = "",
    ) -> AdminEnvironmentScanner:
        return AdminEnvironmentScanner(pinned_cli=pinned_cli, secrets_workdir=session_workdir)

    async def _ensure_initial_environment_scan_started(
        self,
        *,
        bot_app: Any,
        session: Any,
        config_payload: Dict[str, Any],
        chat_id: Optional[int] = None,
        context: Any = None,
    ) -> None:
        await self._start_environment_scan(
            bot_app=bot_app,
            session=session,
            config_payload=config_payload,
            chat_id=chat_id,
            context=context,
            force=False,
            initial=True,
        )

    async def _start_environment_scan(
        self,
        *,
        bot_app: Any,
        session: Any,
        config_payload: Dict[str, Any],
        chat_id: Optional[int],
        context: Any,
        force: bool,
        initial: bool,
    ) -> bool:
        if not force and not self._needs_initial_environment_scan(config_payload):
            return False
        try:
            running_tasks = set(self._mode_task_names(bot_app=bot_app, session=session))
        except Exception:
            self._log.exception("admin scan list mode tasks failed session_id=%s", getattr(session, "id", ""))
            return False
        if self._ENVIRONMENT_SCAN_TASK_NAME in running_tasks:
            return False

        pinned_cli = self._environment_scan_profile_from_config(config_payload)
        if not isinstance(pinned_cli, dict):
            self._log.warning(
                "admin initial scan skipped due to invalid pinned_cli session_id=%s",
                getattr(session, "id", ""),
            )
            return False
        if not pinned_cli:
            self._log.info(
                "admin initial scan skipped because pinned_cli is empty session_id=%s",
                getattr(session, "id", ""),
            )
            return False

        session_id = str(getattr(session, "id", "") or "").strip()
        session_workdir = str(getattr(session, "workdir", "") or "").strip()
        resolved_chat_id = int(chat_id if chat_id is not None else getattr(session, "chat_id", 0) or 0)
        config_store = AdminConfigStore(session_workdir)
        scan_status = "initializing" if initial else "running"
        config_store.update_runtime(scan_status=scan_status, scan_error=None)
        active = bool(resolved_chat_id and self._is_admin_enabled(bot_app=bot_app, session=session, chat_id=resolved_chat_id))
        self._set_runtime_status(
            session=session,
            pipeline_status="initializing" if initial else self._pipeline_status_after_scan(session=session, active=active),
            monitor_status="idle",
            analyzer_status="idle",
            executor_status="idle",
            notifier_status="idle",
            analyzer_message="Environment scan is running.",
            executor_message="No execution requested yet.",
        )
        scanner_builder = self._build_environment_scanner
        scanner_kwargs: Dict[str, Any] = {"pinned_cli": dict(pinned_cli)}
        scanner_signature = inspect.signature(scanner_builder)
        if "session_workdir" in scanner_signature.parameters or any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in scanner_signature.parameters.values()
        ):
            scanner_kwargs["session_workdir"] = session_workdir
        scanner = scanner_builder(**scanner_kwargs)
        messaging = self._messaging(bot_app=bot_app, context=context)

        async def _run_scan() -> None:
            try:
                scan_async = getattr(scanner, "scan_async", None)
                if callable(scan_async):
                    scan_result = await scan_async()
                else:
                    scan_result = await asyncio.to_thread(scanner.scan)
            except asyncio.CancelledError:
                raise
            except Exception:
                config_store.update_runtime(scan_status="failed", scan_error="environment_scan_failed")
                self._set_runtime_status(
                    session=session,
                    pipeline_status="failed" if initial and active else self._pipeline_status_after_scan(session=session, active=active),
                    monitor_status="failed",
                    analyzer_status="idle",
                    executor_status="idle",
                    notifier_status="idle",
                    analyzer_message="Environment scan failed.",
                    executor_message="No execution requested yet.",
                )
                self._log.exception("admin environment scan failed session_id=%s", session_id)
                return

            generated_payload = self._extract_generated_scan_payload(scan_result)
            if not generated_payload:
                config_store.update_runtime(scan_status="failed", scan_error="invalid_scan_payload")
                self._set_runtime_status(
                    session=session,
                    pipeline_status="failed" if initial and active else self._pipeline_status_after_scan(session=session, active=active),
                    monitor_status="failed",
                    analyzer_status="idle",
                    executor_status="idle",
                    notifier_status="idle",
                    analyzer_message="Environment scan returned invalid payload.",
                    executor_message="No execution requested yet.",
                )
                return

            try:
                config_store.apply_scan_result(
                    generated_payload=generated_payload,
                    last_scan_at=time.time(),
                    scan_status="ready",
                    scan_error=None,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                config_store.update_runtime(scan_status="failed", scan_error="persist_generated_scan_failed")
                self._set_runtime_status(
                    session=session,
                    pipeline_status="failed" if initial and active else self._pipeline_status_after_scan(session=session, active=active),
                    monitor_status="failed",
                    analyzer_status="idle",
                    executor_status="idle",
                    notifier_status="idle",
                    analyzer_message="Environment scan could not be persisted.",
                    executor_message="No execution requested yet.",
                )
                self._log.exception("admin scan persist failed session_id=%s", session_id)
                return

            self._set_runtime_status(
                session=session,
                pipeline_status=self._pipeline_status_after_scan(session=session, active=active),
                monitor_status="idle",
                analyzer_status="idle",
                executor_status="idle",
                notifier_status="idle",
                analyzer_message="Environment scan completed.",
                executor_message="No execution requested yet.",
            )
            if active and resolved_chat_id:
                await self._ensure_runner_started(
                    bot_app=bot_app,
                    session=session,
                    chat_id=resolved_chat_id,
                    messaging=messaging,
                    context=context,
                )

        try:
            tasks = self.get_service("tasks")
            if tasks is None:
                raise RuntimeError("tasks service is not configured")
            task = tasks.create(
                session_uid=session_runtime_uid(session),
                mode_id=self.get_mode_id(),
                coro=_run_scan(),
                name=self._ENVIRONMENT_SCAN_TASK_NAME,
            )
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=0.2)
            except asyncio.TimeoutError:
                pass
        except Exception:
            self._log.exception("admin initial scan start failed session_id=%s", session_id)
            return False
        return True

    @staticmethod
    def _extract_generated_scan_payload(scan_result: Any) -> Dict[str, Any]:
        if not isinstance(scan_result, Mapping):
            return {}
        generated = scan_result.get("generated")
        if isinstance(generated, Mapping) and generated:
            return dict(generated)
        generated_payload: Dict[str, Any] = {}
        for key in (
            "environment",
            "diagnostics",
            "incidents",
            "actions",
            "policies",
            "monitor",
            "scan_meta",
            "scan_summary",
        ):
            value = scan_result.get(key)
            if isinstance(value, Mapping):
                generated_payload[key] = dict(value)
        return generated_payload

    def _pipeline_status_after_scan(self, *, session: Any, active: bool) -> str:
        if not active:
            return "disabled"
        current = self._normalize_pipeline_status(
            self._runtime_status_snapshot(session).get("pipeline_status"),
            default="idle",
        )
        if current in {"running", "completed", "failed"}:
            return current
        return "idle"

    @staticmethod
    def _format_entities_list(*, title: str, rows: list[Dict[str, Any]], id_key: str) -> str:
        if not rows:
            return f"{title}: нет данных"
        lines = [title]
        for index, row in enumerate(rows, start=1):
            entity_id = str(row.get(id_key) or "-")
            payload = row.get("payload", {})
            lines.append(f"{index}. {entity_id} | payload={payload}")
        return "\n".join(lines)

    @staticmethod
    def _iter_servers(*, config_payload: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        admin_cfg = config_payload.get("admin", {}) if isinstance(config_payload, dict) else {}
        if not isinstance(admin_cfg, dict):
            return {}
        raw = admin_cfg.get("servers", {})
        out: Dict[str, Dict[str, Any]] = {}
        if isinstance(raw, dict):
            for server_id, payload in raw.items():
                sid = str(server_id or "").strip()
                if not sid:
                    continue
                row = dict(payload or {}) if isinstance(payload, dict) else {}
                row.setdefault("id", sid)
                out[sid] = row
            return out
        if isinstance(raw, list):
            for payload in raw:
                if not isinstance(payload, dict):
                    continue
                sid = str(payload.get("id") or payload.get("server_id") or "").strip()
                if not sid:
                    continue
                out[sid] = dict(payload)
        return out

    def _resolve_server_config(self, *, config_payload: Dict[str, Any], server_id: str) -> Optional[Dict[str, Any]]:
        return self._iter_servers(config_payload=config_payload).get(str(server_id or "").strip())

    def _resolve_check_action_id(self, *, config_payload: Dict[str, Any], server_id: str) -> str:
        server_cfg = self._resolve_server_config(config_payload=config_payload, server_id=server_id) or {}
        action_id = str(server_cfg.get("check_action_id") or "").strip()
        if action_id:
            return action_id
        admin_cfg = config_payload.get("admin", {}) if isinstance(config_payload, dict) else {}
        monitor_cfg = admin_cfg.get("monitor", {}) if isinstance(admin_cfg, dict) else {}
        monitor_servers = monitor_cfg.get("servers", []) if isinstance(monitor_cfg, dict) else []
        if isinstance(monitor_servers, list):
            for row in monitor_servers:
                if not isinstance(row, dict):
                    continue
                sid = str(row.get("id") or row.get("server_id") or "").strip()
                if sid != str(server_id or "").strip():
                    continue
                action_id = str(row.get("action_id") or "").strip()
                if action_id:
                    return action_id
        return ""

    def _resolve_action_payload_for_server(
        self,
        *,
        config_payload: Dict[str, Any],
        action_id: str,
        server_id: str,
    ) -> tuple[str, Any]:
        server_cfg = self._resolve_server_config(config_payload=config_payload, server_id=server_id)
        if not isinstance(server_cfg, dict):
            raise ValueError(f"Server `{server_id}` не найден в admin.servers.")
        target = str(server_cfg.get("target") or "").strip().lower()
        if target not in self._EXEC_TARGETS:
            raise ValueError(f"Server `{server_id}` имеет неподдерживаемый target `{target}`.")
        if not is_action_allowlisted(config_payload, target=target, action_id=action_id):
            raise ValueError(f"Action `{action_id}` не входит в allowlist для server `{server_id}`.")

        action_payload = self._resolve_exec_action_payload(
            config_payload=config_payload,
            target=target,
            action_id=action_id,
        )
        if action_payload is None:
            raise ValueError(f"Action `{action_id}` не имеет конфигурации команды.")
        if target == "ssh":
            if not isinstance(action_payload, dict):
                raise ValueError(f"Action `{action_id}` для ssh должен быть mapping.")
            merged = dict(action_payload)
            for key in ("host", "port", "user", "key_path", "password_env", "options"):
                if key in server_cfg and server_cfg.get(key) not in (None, ""):
                    merged[key] = server_cfg.get(key)
            action_payload = merged
        return target, action_payload

    def _resolve_exec_action_payload(
        self,
        *,
        config_payload: Dict[str, Any],
        target: str,
        action_id: str,
    ) -> Any:
        return _action_resolve_payload(
            config_payload=config_payload,
            target=target,
            action_id=action_id,
        )

    def _build_local_command_spec(
        self,
        *,
        session: Any,
        action_id: str,
        action_payload: Any,
    ) -> LocalCommandSpec:
        return _action_build_local_spec(
            session=session,
            action_id=action_id,
            action_payload=action_payload,
        )

    def _build_ssh_command_spec(
        self,
        *,
        session: Any,
        action_id: str,
        action_payload: Any,
    ) -> SSHCommandSpec:
        return _action_build_ssh_spec(
            session=session,
            action_id=action_id,
            action_payload=action_payload,
        )

    async def _input_run_local(
        self,
        *,
        bot_app: Any,
        session: Any,
        chat_id: int,
        ms: Any,
        action_id: str,
    ) -> ToolResult:
        cfg_payload = self._load_admin_config(bot_app=bot_app, session=session)
        action_payload = self._resolve_exec_action_payload(
            config_payload=cfg_payload,
            target="local",
            action_id=action_id,
        )
        if action_payload is None:
            return await self._input_show_error(
                chat_id=chat_id,
                ms=ms,
                text=f"Для action `{action_id}` не найден конфиг команды local.",
            )

        try:
            spec = self._build_local_command_spec(
                session=session,
                action_id=action_id,
                action_payload=action_payload,
            )
            result = await self._local_transport.run(spec)
        except (ValueError, LocalTransportError) as exc:
            self._log.exception("admin local execution failed action_id=%s", action_id)
            return await self._input_show_error(
                chat_id=chat_id,
                ms=ms,
                text=f"Local action `{action_id}` завершился с ошибкой: {exc}",
            )
        except Exception as exc:
            self._log.exception("admin local execution unexpected failure action_id=%s", action_id)
            return await self._input_show_error(
                chat_id=chat_id,
                ms=ms,
                text=f"Local action `{action_id}` завершился с неожиданной ошибкой: {exc}",
            )

        chunks = [
            f"Local action: {result.action_id}",
            f"Exit code: {result.returncode}",
            f"Duration: {result.duration_ms}ms",
        ]
        if result.timed_out:
            chunks.append("Timed out: yes")
        if result.stdout.strip():
            chunks.append(f"STDOUT:\n{result.stdout.strip()}")
        if result.stderr.strip():
            chunks.append(f"STDERR:\n{result.stderr.strip()}")
        text = "\n\n".join(chunks)
        await ms.send_text(chat_id, text, md2=True)
        if result.returncode == 0 and not result.timed_out:
            return ToolResult.ok(text)
        return ToolResult.fail("admin_local_action_failed")

    async def _input_run_ssh(
        self,
        *,
        bot_app: Any,
        session: Any,
        chat_id: int,
        ms: Any,
        action_id: str,
    ) -> ToolResult:
        cfg_payload = self._load_admin_config(bot_app=bot_app, session=session)
        action_payload = self._resolve_exec_action_payload(
            config_payload=cfg_payload,
            target="ssh",
            action_id=action_id,
        )
        if action_payload is None:
            return await self._input_show_error(
                chat_id=chat_id,
                ms=ms,
                text=f"Для action `{action_id}` не найден конфиг команды ssh.",
            )

        try:
            spec = self._build_ssh_command_spec(
                session=session,
                action_id=action_id,
                action_payload=action_payload,
            )
            result = await self._ssh_transport.run(spec)
        except (ValueError, SSHTransportError) as exc:
            self._log.exception("admin ssh execution failed action_id=%s", action_id)
            return await self._input_show_error(
                chat_id=chat_id,
                ms=ms,
                text=f"SSH action `{action_id}` завершился с ошибкой: {exc}",
            )
        except Exception as exc:
            self._log.exception("admin ssh execution unexpected failure action_id=%s", action_id)
            return await self._input_show_error(
                chat_id=chat_id,
                ms=ms,
                text=f"SSH action `{action_id}` завершился с неожиданной ошибкой: {exc}",
            )

        target = f"{result.user + '@' if result.user else ''}{result.host}:{result.port}"
        chunks = [
            f"SSH action: {result.action_id}",
            f"Target: {target}",
            f"Exit code: {result.returncode}",
            f"Duration: {result.duration_ms}ms",
        ]
        if result.timed_out:
            chunks.append("Timed out: yes")
        if result.stdout.strip():
            chunks.append(f"STDOUT:\n{result.stdout.strip()}")
        if result.stderr.strip():
            chunks.append(f"STDERR:\n{result.stderr.strip()}")
        text = "\n\n".join(chunks)
        await ms.send_text(chat_id, text, md2=True)
        if result.returncode == 0 and not result.timed_out:
            return ToolResult.ok(text)
        return ToolResult.fail("admin_ssh_action_failed")

    async def _input_execute_action(
        self,
        *,
        bot_app: Any,
        session: Any,
        chat_id: int,
        user_id: Any,
        ms: Any,
        payload: Dict[str, Any],
    ) -> ToolResult:
        command = str(payload.get("command") or "").strip().lower()
        server_id = str(payload.get("server_id") or "").strip()
        action_id = str(payload.get("action_id") or "").strip()
        if command not in {"check", "run"}:
            return await self._input_show_error(
                chat_id=chat_id,
                ms=ms,
                text="Некорректный payload команды выполнения.",
            )
        if not server_id or not is_valid_action_id(server_id):
            return await self._input_show_error(
                chat_id=chat_id,
                ms=ms,
                text="Некорректный server_id.",
            )
        if command == "run" and (not action_id or not is_valid_action_id(action_id)):
            return await self._input_show_error(
                chat_id=chat_id,
                ms=ms,
                text="Некорректный action_id.",
            )
        if not self._is_session_selected_for_chat(bot_app=bot_app, session=session):
            self._log.warning(
                "admin policy violation: run/check requested outside selected session sid=%s chat_id=%s",
                getattr(session, "id", ""),
                chat_id,
            )
            return await self._input_show_error(
                chat_id=chat_id,
                ms=ms,
                text="Действие разрешено только в текущей выбранной сессии.",
            )
        if not self._is_admin_enabled(bot_app=bot_app, session=session, chat_id=str(chat_id)):
            return await self._input_show_error(
                chat_id=chat_id,
                ms=ms,
                text="Admin выключен для текущей сессии. Сначала выполните `/admin enable`.",
            )

        cfg_payload = self._load_admin_config(bot_app=bot_app, session=session)
        if command == "check":
            action_id = self._resolve_check_action_id(config_payload=cfg_payload, server_id=server_id)
            if not action_id:
                self._log.warning(
                    "admin policy violation: check without configured action server_id=%s session_id=%s",
                    server_id,
                    getattr(session, "id", ""),
                )
                return await self._input_show_error(
                    chat_id=chat_id,
                    ms=ms,
                    text=f"Для server `{server_id}` не задан check_action_id.",
                )
        try:
            target, action_payload = self._resolve_action_payload_for_server(
                config_payload=cfg_payload,
                action_id=action_id,
                server_id=server_id,
            )
        except Exception as exc:
            self._log.warning(
                "admin policy violation: run/check blocked sid=%s server_id=%s action_id=%s reason=%s",
                getattr(session, "id", ""),
                server_id,
                action_id,
                exc,
            )
            return await self._input_show_error(
                chat_id=chat_id,
                ms=ms,
                text=str(exc),
            )

        local_spec: Optional[LocalCommandSpec] = None
        ssh_spec: Optional[SSHCommandSpec] = None
        state_store = None
        run: Optional[RunArtifactHandle] = None
        current_phase = "complete"
        operation_payload: Dict[str, Any] = {}
        execution_context_payload: Dict[str, Any] = {}
        try:
            state_store = self._get_state_store(bot_app=bot_app)
        except Exception:
            self._log.exception("admin state store init failed for execute path session_id=%s", getattr(session, "id", ""))
        try:
            if target == "local":
                local_spec = self._build_local_command_spec(
                    session=session,
                    action_id=action_id,
                    action_payload=action_payload,
                )
            else:
                ssh_spec = self._build_ssh_command_spec(
                    session=session,
                    action_id=action_id,
                    action_payload=action_payload,
                )
            session_state = self._get_admin_session_state(bot_app=bot_app, session=session, chat_id=str(chat_id))
            session_dry_run = bool(session_state.get("dry_run", True))
            effective_dry_run = bool(command == "check" or session_dry_run)
            normalized_command = ""
            if local_spec is not None:
                normalized_command = " ".join(str(item) for item in local_spec.argv)
            elif ssh_spec is not None:
                normalized_command = " ".join(str(item) for item in ssh_spec.argv)
            try:
                import hashlib

                command_hash = hashlib.sha256(normalized_command.encode("utf-8")).hexdigest()
            except Exception:
                command_hash = ""

            admin_cfg = cfg_payload.get("admin", {}) if isinstance(cfg_payload, dict) else {}
            policies_cfg = admin_cfg.get("policies", {}) if isinstance(admin_cfg, dict) else {}
            action_policies = {}
            if isinstance(policies_cfg, dict):
                per_action = policies_cfg.get("per_action", {})
                if isinstance(per_action, dict):
                    candidate = per_action.get(action_id, {})
                    if isinstance(candidate, dict):
                        action_policies = dict(candidate)
            risk_level = "medium"
            if isinstance(action_payload, dict):
                risk_level = str(action_payload.get("risk_level") or risk_level).strip().lower() or risk_level
            execution_context = AdminExecutionContext(
                command=command,
                action_id=action_id,
                target=target,
                dry_run=bool(effective_dry_run),
                check_only=bool(command == "check"),
                session_id=str(getattr(session, "id", "") or ""),
                chat_id=str(chat_id),
                user_id=int(user_id) if isinstance(user_id, int) else None,
                flags={
                    "server_id": server_id,
                    "risk_level": risk_level,
                    "normalized_command": normalized_command,
                    "command_hash": command_hash,
                    "dry_run_state": bool(session_dry_run),
                    "cooldown_sec": action_policies.get("cooldown_sec", policies_cfg.get("cooldown_sec", 0)),
                    "rate_limit_max": action_policies.get(
                        "rate_limit_max",
                        policies_cfg.get("auto_actions_per_hour", 0),
                    ),
                    "rate_limit_window_sec": action_policies.get("rate_limit_window_sec", 3600),
                    "maintenance_window": action_policies.get(
                        "maintenance_window",
                        policies_cfg.get("maintenance_window", {}),
                    ),
                    "mandatory_notify_actions": policies_cfg.get(
                        "mandatory_notify_actions",
                        [],
                    ),
                    "policy_version": str(policies_cfg.get("version") or "v1"),
                },
            )
            operation_payload = {
                "kind": f"manual_{command}",
                "command": command,
                "action_id": action_id,
                "server_id": server_id,
                "target_transport": target,
            }
            execution_context_payload = {
                "native_transport_execution": True,
                "skill_selector_bypassed": True,
                "skill_selector_bypass_reason": "native_admin_transport",
                "destructive_execution": bool(command == "run" and not effective_dry_run),
                "dry_run": bool(effective_dry_run),
                "check_only": bool(command == "check"),
                "action_id": action_id,
                "server_id": server_id,
                "risk_level": risk_level,
                "command_source": "native_admin_transport",
            }
            run, _resume_guard = self._prepare_run_artifacts(
                session=session,
                source_text=f"/admin {command} {action_id} {server_id}".strip(),
                phase="complete",
                operation_payload=operation_payload,
                mode_context={
                    "target_transport": target,
                    "execution_context": execution_context_payload,
                },
            )
            self._save_run_plan(
                run,
                self._operation_plan_payload(operation_payload=operation_payload, phase="complete"),
            )
            self._append_checkpoint(
                run,
                self._checkpoint_payload(
                    phase="complete",
                    status="started",
                    operation_payload=operation_payload,
                    target_transport=target,
                    message=f"{command}:{action_id}:{server_id}",
                ),
            )
            self._append_run_event(
                run,
                {
                    "event_type": "admin_manual_operation_start",
                    "operation_kind": str(operation_payload.get("kind") or ""),
                    "phase": "complete",
                    "target_transport": target,
                    "action_id": action_id,
                    "server_id": server_id,
                },
            )
            result = await self._executor.execute(
                context=execution_context,
                local_transport=self._local_transport,
                ssh_transport=self._ssh_transport,
                local_spec=local_spec,
                ssh_spec=ssh_spec,
                state_store=state_store,
            )
            final_status = "completed" if bool(result.success) else "failed"
            self._save_run_state(
                run,
                phase="complete",
                status=final_status,
                mode_context={
                    "operation_payload": dict(operation_payload),
                    "target_transport": target,
                    "execution_context": dict(execution_context_payload),
                    "last_action_result": self._summarize_execution_result(result),
                },
            )
            self._append_checkpoint(
                run,
                self._checkpoint_payload(
                    phase="complete",
                    status="ok" if bool(result.success) else "error",
                    operation_payload=operation_payload,
                    target_transport=target,
                    message=str(result.text or ""),
                ),
            )
            self._validate_run_boundary(run, phase="complete")
            self._mark_run_finished(run, status=final_status, phase="complete")
            self._append_run_event(
                run,
                {
                    "event_type": "admin_manual_operation_complete",
                    "operation_kind": str(operation_payload.get("kind") or ""),
                    "phase": "complete",
                    "status": final_status,
                    "target_transport": target,
                    "action_id": action_id,
                    "server_id": server_id,
                },
            )
        except (ValueError, LocalTransportError, SSHTransportError, AdminExecutorError) as exc:
            self._save_run_state(
                run,
                phase=current_phase,
                status="failed",
                mode_context={
                    "operation_payload": dict(operation_payload or {}),
                    "target_transport": target if "target" in locals() else "",
                    "execution_context": dict(execution_context_payload or {}),
                    "runtime_error": str(exc or ""),
                },
            )
            self._append_checkpoint(
                run,
                self._checkpoint_payload(
                    phase=current_phase,
                    status="error",
                    operation_payload=operation_payload,
                    target_transport=target if "target" in locals() else "",
                    message=str(exc or ""),
                ),
            )
            self._append_run_event(
                run,
                {
                    "event_type": "admin_manual_operation_failed",
                    "operation_kind": str((operation_payload or {}).get("kind") or ""),
                    "phase": current_phase,
                    "status": "failed",
                    "error": str(exc or ""),
                },
            )
            self._mark_run_finished(run, status="failed", phase=current_phase)
            self._log.exception("admin execute command failed action_id=%s command=%s", action_id, command)
            return await self._input_show_error(
                chat_id=chat_id,
                ms=ms,
                text=f"Команда `{command}` для action `{action_id}` завершилась с ошибкой: {exc}",
            )
        except Exception as exc:
            self._save_run_state(
                run,
                phase=current_phase,
                status="failed",
                mode_context={
                    "operation_payload": dict(operation_payload or {}),
                    "target_transport": target if "target" in locals() else "",
                    "execution_context": dict(execution_context_payload or {}),
                    "runtime_error": str(exc or ""),
                },
            )
            self._append_checkpoint(
                run,
                self._checkpoint_payload(
                    phase=current_phase,
                    status="error",
                    operation_payload=operation_payload,
                    target_transport=target if "target" in locals() else "",
                    message=str(exc or ""),
                ),
            )
            self._append_run_event(
                run,
                {
                    "event_type": "admin_manual_operation_failed",
                    "operation_kind": str((operation_payload or {}).get("kind") or ""),
                    "phase": current_phase,
                    "status": "failed",
                    "error": str(exc or ""),
                },
            )
            self._mark_run_finished(run, status="failed", phase=current_phase)
            self._log.exception("admin execute command unexpected failure action_id=%s command=%s", action_id, command)
            return await self._input_show_error(
                chat_id=chat_id,
                ms=ms,
                text=f"Команда `{command}` для action `{action_id}` завершилась с неожиданной ошибкой: {exc}",
            )
        finally:
            self._clear_active_run_handle(session)

        await ms.send_text(chat_id, str(result.text or ""), md2=True)
        if bool(result.success):
            return ToolResult.ok(str(result.text or ""))
        return ToolResult.fail("admin_execute_action_failed")

    async def _input_list_incidents(
        self,
        *,
        bot_app: Any,
        session: Any,
        chat_id: int,
        ms: Any,
        payload: Dict[str, Any],
    ) -> ToolResult:
        limit = int(payload.get("limit") or 20)
        try:
            store = self._get_state_store(bot_app=bot_app)
            rows = store.list_incidents(
                str(getattr(session, "id", "") or ""),
                chat_id=str(chat_id),
                limit=max(1, min(limit, 200)),
            )
        except Exception:
            self._log.exception("admin incidents list failed session_id=%s", getattr(session, "id", ""))
            return await self._input_show_error(
                chat_id=chat_id,
                ms=ms,
                text="Не удалось получить список incidents из БД.",
            )
        text = self._format_entities_list(title="Incidents", rows=rows, id_key="incident_id")
        await ms.send_text(chat_id, text, md2=True)
        return ToolResult.ok(text)

    async def _input_list_actions(
        self,
        *,
        bot_app: Any,
        session: Any,
        chat_id: int,
        ms: Any,
        payload: Dict[str, Any],
    ) -> ToolResult:
        limit = int(payload.get("limit") or 20)
        try:
            store = self._get_state_store(bot_app=bot_app)
            rows = store.list_actions(
                str(getattr(session, "id", "") or ""),
                chat_id=str(chat_id),
                limit=max(1, min(limit, 200)),
            )
        except Exception:
            self._log.exception("admin actions list failed session_id=%s", getattr(session, "id", ""))
            return await self._input_show_error(
                chat_id=chat_id,
                ms=ms,
                text="Не удалось получить список actions из БД.",
            )
        text = self._format_entities_list(title="Actions", rows=rows, id_key="action_id")
        await ms.send_text(chat_id, text, md2=True)
        return ToolResult.ok(text)

    async def _input_ack_alert(
        self,
        *,
        bot_app: Any,
        session: Any,
        chat_id: int,
        user_id: Any,
        ms: Any,
        incident_id: str,
    ) -> ToolResult:
        sid = str(getattr(session, "id", "") or "").strip()
        iid = str(incident_id or "").strip()
        if not sid or not iid:
            return await self._input_show_error(
                chat_id=chat_id,
                ms=ms,
                text="Не удалось выполнить ack: отсутствует session_id или incident_id.",
            )

        now_ts = float(time.time())
        try:
            store = self._get_state_store(bot_app=bot_app)
            incident = store.get_incident(iid, chat_id=str(chat_id))
            if not isinstance(incident, dict) or str(incident.get("session_id") or "") != sid:
                self._log.warning(
                    "admin policy violation: ack incident outside current session sid=%s incident_id=%s",
                    sid,
                    iid,
                )
                return await self._input_show_error(
                    chat_id=chat_id,
                    ms=ms,
                    text=f"Incident `{iid}` не найден в текущей сессии.",
                )
            incident_payload = dict(incident.get("payload") or {})
            alert_id = str(incident_payload.get("alert_id") or "").strip()
            if not alert_id:
                return await self._input_show_error(
                    chat_id=chat_id,
                    ms=ms,
                    text=f"Для incident `{iid}` отсутствует связанный alert_id.",
                )

            ack_id = f"{sid}:{iid}"
            ack_payload: Dict[str, Any] = {
                "incident_id": iid,
                "alert_id": alert_id,
                "acked_at": now_ts,
            }
            if isinstance(user_id, int):
                ack_payload["acked_by_user_id"] = int(user_id)
            ack_row = store.create_acknowledgement(
                ack_id,
                session_id=sid,
                chat_id=str(chat_id),
                payload=ack_payload,
            )

            current_alert = store.get_alert_state(alert_id, chat_id=str(chat_id)) or {}
            alert_payload = dict(current_alert.get("payload") or {})
            alert_payload.update(
                {
                    "acknowledged": True,
                    "acknowledged_at": now_ts,
                    "acknowledgement_id": str(ack_row.get("acknowledgement_id") or ack_id),
                    "incident_id": iid,
                }
            )
            if current_alert:
                store.update_alert_state(
                    alert_id,
                    session_id=sid,
                    chat_id=str(chat_id),
                    payload=alert_payload,
                )
            else:
                store.create_alert_state(
                    alert_id,
                    session_id=sid,
                    chat_id=str(chat_id),
                    payload=alert_payload,
                )
        except Exception:
            self._log.exception("admin ack failed session_id=%s incident_id=%s", sid, iid)
            return await self._input_show_error(
                chat_id=chat_id,
                ms=ms,
                text=f"Не удалось подтвердить incident `{iid}`.",
            )

        text = f"ACK выполнен: incident_id={iid}"
        await ms.send_text(chat_id, text, md2=True)
        return ToolResult.ok(text)

    async def _input_set_dry_run(
        self,
        *,
        bot_app: Any,
        session: Any,
        chat_id: int,
        user_id: Any,
        ms: Any,
        payload: Dict[str, Any],
    ) -> ToolResult:
        dry_run = bool(payload.get("dry_run"))
        try:
            self._upsert_session_status(
                bot_app=bot_app,
                session=session,
                chat_id=str(chat_id),
                dry_run=dry_run,
                updated_by=int(user_id) if isinstance(user_id, int) else None,
            )
        except Exception:
            self._log.exception(
                "admin dry-run toggle failed session_id=%s dry_run=%s",
                getattr(session, "id", ""),
                dry_run,
            )
            return await self._input_show_error(
                chat_id=chat_id,
                ms=ms,
                text="Не удалось обновить dry-run состояние.",
            )
        self._set_session_admin_enabled(session=session, enabled=self._is_admin_enabled(bot_app=bot_app, session=session, chat_id=chat_id))
        text = "Dry-run: on" if dry_run else "Dry-run: off"
        await ms.send_text(chat_id, text, md2=True)
        return ToolResult.ok(text)

    async def _input_mute_alerts(
        self,
        *,
        bot_app: Any,
        session: Any,
        chat_id: int,
        ms: Any,
        payload: Dict[str, Any],
    ) -> ToolResult:
        sid = str(getattr(session, "id", "") or "").strip()
        minutes = float(payload.get("minutes") or 60.0)
        muted_until_ts = float(time.time() + max(1.0, minutes) * 60.0)
        try:
            store = self._get_state_store(bot_app=bot_app)
            store.mute_session(sid, muted_until_ts, chat_id=str(chat_id))
        except Exception:
            self._log.exception("admin mute failed session_id=%s", sid)
            return await self._input_show_error(
                chat_id=chat_id,
                ms=ms,
                text="Не удалось установить mute для alerts.",
            )
        text = f"Alerts muted until_ts={muted_until_ts:.3f}"
        await ms.send_text(chat_id, text, md2=True)
        return ToolResult.ok(text)

    async def _input_unmute_alerts(
        self,
        *,
        bot_app: Any,
        session: Any,
        chat_id: int,
        ms: Any,
    ) -> ToolResult:
        sid = str(getattr(session, "id", "") or "").strip()
        try:
            store = self._get_state_store(bot_app=bot_app)
            store.unmute_session(sid, chat_id=str(chat_id))
        except Exception:
            self._log.exception("admin unmute failed session_id=%s", sid)
            return await self._input_show_error(
                chat_id=chat_id,
                ms=ms,
                text="Не удалось снять mute для alerts.",
            )
        text = "Alerts unmuted."
        await ms.send_text(chat_id, text, md2=True)
        return ToolResult.ok(text)

    async def _input_list_approvals(
        self,
        *,
        bot_app: Any,
        session: Any,
        chat_id: int,
        ms: Any,
    ) -> ToolResult:
        try:
            store = self._get_state_store(bot_app=bot_app)
            rows = store.list_approved_overrides(
                str(getattr(session, "id", "") or ""),
                chat_id=str(chat_id),
                limit=50,
            )
        except Exception:
            self._log.exception("admin approvals list failed session_id=%s", getattr(session, "id", ""))
            return await self._input_show_error(
                chat_id=chat_id,
                ms=ms,
                text="Не удалось получить список approvals из БД.",
            )
        text = self._format_entities_list(title="Approvals", rows=rows, id_key="override_id")
        await ms.send_text(chat_id, text, md2=True)
        return ToolResult.ok(text)

    async def _input_revoke_approval(
        self,
        *,
        bot_app: Any,
        session: Any,
        chat_id: int,
        ms: Any,
        override_id: str,
    ) -> ToolResult:
        sid = str(getattr(session, "id", "") or "").strip()
        oid = str(override_id or "").strip()
        try:
            store = self._get_state_store(bot_app=bot_app)
            existing = store.get_approved_override(oid, chat_id=str(chat_id))
            if not isinstance(existing, dict) or str(existing.get("session_id") or "") != sid:
                self._log.warning(
                    "admin policy violation: approval revoke outside session sid=%s override_id=%s",
                    sid,
                    oid,
                )
                return await self._input_show_error(
                    chat_id=chat_id,
                    ms=ms,
                    text=f"Approval `{oid}` не найден в текущей сессии.",
                )
            deleted = bool(store.delete_approved_override(oid))
            if not deleted:
                return await self._input_show_error(
                    chat_id=chat_id,
                    ms=ms,
                    text=f"Не удалось удалить approval `{oid}`.",
                )
        except Exception:
            self._log.exception(
                "admin approval revoke failed session_id=%s override_id=%s",
                sid,
                oid,
            )
            return await self._input_show_error(
                chat_id=chat_id,
                ms=ms,
                text=f"Не удалось отозвать approval `{oid}`.",
            )

        text = f"Approval revoked: override_id={oid}"
        await ms.send_text(chat_id, text, md2=True)
        return ToolResult.ok(text)

    async def _input_clear_approvals(
        self,
        *,
        bot_app: Any,
        session: Any,
        chat_id: int,
        ms: Any,
    ) -> ToolResult:
        sid = str(getattr(session, "id", "") or "").strip()
        try:
            store = self._get_state_store(bot_app=bot_app)
            removed = int(store.clear_approved_overrides(sid, chat_id=str(chat_id)))
        except Exception:
            self._log.exception("admin approvals clear failed session_id=%s", sid)
            return await self._input_show_error(
                chat_id=chat_id,
                ms=ms,
                text="Не удалось очистить approvals в текущей сессии.",
            )

        text = f"Approvals cleared: {removed}"
        await ms.send_text(chat_id, text, md2=True)
        return ToolResult.ok(text)

    async def _input_list_skill_installs(
        self,
        *,
        bot_app: Any,
        session: Any,
        chat_id: int,
        ms: Any,
    ) -> ToolResult:
        skill_runtime = self._resolve_skill_runtime(bot_app=bot_app)
        if skill_runtime is None:
            return await self._input_show_error(
                chat_id=chat_id,
                ms=ms,
                text="Skill runtime недоступен для списка pending installs.",
            )
        try:
            rows = [
                self._serialize_pending_skill_install(item)
                for item in reversed(list(skill_runtime.list_pending_installs(session=session) or []))
            ]
        except Exception:
            self._log.exception("admin skills list failed session_id=%s", getattr(session, "id", ""))
            return await self._input_show_error(
                chat_id=chat_id,
                ms=ms,
                text="Не удалось получить список pending skill installs.",
            )
        if not rows:
            text = "Skill approvals: нет данных"
        else:
            lines = ["Skill approvals"]
            for index, row in enumerate(rows, start=1):
                approval_id = str(row.get("approval_id") or "-")
                skill_id = str(row.get("skill_id") or "-")
                source = str(row.get("source") or "-")
                mode_phase = "/".join(
                    token
                    for token in (
                        str(row.get("mode_id") or "").strip(),
                        str(row.get("phase") or "").strip(),
                    )
                    if token
                )
                detail_parts = [f"skill={skill_id}"]
                if mode_phase:
                    detail_parts.append(f"mode={mode_phase}")
                if source and source != "-":
                    detail_parts.append(f"source={source}")
                lines.append(f"{index}. {approval_id} | {' | '.join(detail_parts)}")
            text = "\n".join(lines)
        await ms.send_text(chat_id, text, md2=True)
        return ToolResult.ok(text)

    async def _input_approve_skill_install(
        self,
        *,
        bot_app: Any,
        session: Any,
        chat_id: int,
        ms: Any,
        approval_id: str,
    ) -> ToolResult:
        skill_runtime = self._resolve_skill_runtime(bot_app=bot_app)
        if skill_runtime is None:
            return await self._input_show_error(
                chat_id=chat_id,
                ms=ms,
                text="Skill runtime недоступен для approve skill install.",
            )
        try:
            result = skill_runtime.approve_pending_install(
                session=session,
                approval_id=str(approval_id or "").strip(),
                actor_chat_id=chat_id,
                access_policy=getattr(bot_app, "access_policy_service", None),
            )
        except Exception:
            self._log.exception(
                "admin skill approve failed session_id=%s approval_id=%s",
                getattr(session, "id", ""),
                approval_id,
            )
            return await self._input_show_error(
                chat_id=chat_id,
                ms=ms,
                text=f"Не удалось подтвердить skill install `{approval_id}`.",
            )
        text = str(result.message or "").strip() or "Skill install approve completed."
        await ms.send_text(chat_id, text, md2=True)
        return ToolResult.ok(text)

    async def _input_reject_skill_install(
        self,
        *,
        bot_app: Any,
        session: Any,
        chat_id: int,
        ms: Any,
        approval_id: str,
    ) -> ToolResult:
        skill_runtime = self._resolve_skill_runtime(bot_app=bot_app)
        if skill_runtime is None:
            return await self._input_show_error(
                chat_id=chat_id,
                ms=ms,
                text="Skill runtime недоступен для reject skill install.",
            )
        try:
            result = skill_runtime.reject_pending_install(
                session=session,
                approval_id=str(approval_id or "").strip(),
                actor_chat_id=chat_id,
                access_policy=getattr(bot_app, "access_policy_service", None),
            )
        except Exception:
            self._log.exception(
                "admin skill reject failed session_id=%s approval_id=%s",
                getattr(session, "id", ""),
                approval_id,
            )
            return await self._input_show_error(
                chat_id=chat_id,
                ms=ms,
                text=f"Не удалось отклонить skill install `{approval_id}`.",
            )
        text = str(result.message or "").strip() or "Skill install rejected."
        await ms.send_text(chat_id, text, md2=True)
        return ToolResult.ok(text)

    async def _input_show_menu(
        self,
        *,
        bot_app: Any,
        session: Any,
        chat_id: int,
        ms: Any,
    ) -> ToolResult:
        self._set_session_admin_enabled(
            session=session,
            enabled=self._is_admin_enabled(bot_app=bot_app, session=session, chat_id=str(chat_id)),
        )
        text, keyboard = self.build_menu(
            session,
            back_callback="sess_active",
            back_text="⬅️ Назад",
        )
        await ms.send_text(chat_id, text, md2=True, reply_markup=keyboard)
        return ToolResult.ok()

    async def _input_enable_mode(
        self,
        *,
        bot_app: Any,
        session: Any,
        chat_id: int,
        user_id: Any,
        ms: Any,
        context: Any,
    ) -> ToolResult:
        try:
            await self._activate_admin_runtime(
                bot_app=bot_app,
                session=session,
                chat_id=chat_id,
                user_id=user_id,
                context=context,
                ms=ms,
            )
        except Exception:
            self._log.exception("admin input enable failed session_id=%s", getattr(session, "id", ""))
            return await self._input_show_error(
                chat_id=chat_id,
                ms=ms,
                text="Не удалось включить Admin режим.",
            )
        text = "Admin включен."
        await ms.send_text(chat_id, text, md2=True)
        return ToolResult.ok(text)

    async def _input_disable_mode(
        self,
        *,
        bot_app: Any,
        session: Any,
        chat_id: int,
        user_id: Any,
        ms: Any,
    ) -> ToolResult:
        try:
            await self._deactivate_admin_runtime(
                bot_app=bot_app,
                session=session,
                chat_id=chat_id,
                user_id=user_id,
            )
        except Exception:
            self._log.exception("admin input disable failed session_id=%s", getattr(session, "id", ""))
            return await self._input_show_error(
                chat_id=chat_id,
                ms=ms,
                text="Не удалось выключить Admin режим.",
            )

        text = "Admin выключен."
        await ms.send_text(chat_id, text, md2=True)
        return ToolResult.ok(text)

    async def _input_rescan_environment(
        self,
        *,
        bot_app: Any,
        session: Any,
        chat_id: int,
        user_id: Any,
        ms: Any,
        context: Any,
    ) -> ToolResult:
        _ = user_id
        try:
            started = await self._start_environment_scan(
                bot_app=bot_app,
                session=session,
                config_payload=self._load_admin_config(bot_app=bot_app, session=session, effective=False),
                chat_id=chat_id,
                context=context,
                force=True,
                initial=False,
            )
        except Exception:
            self._log.exception("admin input rescan failed session_id=%s", getattr(session, "id", ""))
            return await self._input_show_error(
                chat_id=chat_id,
                ms=ms,
                text="Не удалось запустить пересканирование окружения.",
            )

        text = "Rescan already running." if not started else "Environment rescan started."
        await ms.send_text(chat_id, text, md2=True)
        return ToolResult.ok(text)

    async def _input_handle_chat(
        self,
        *,
        bot_app: Any,
        session: Any,
        chat_id: int,
        user_id: Any,
        ms: Any,
        user_text: str,
    ) -> ToolResult:
        _ = user_id
        if not self._is_admin_enabled(bot_app=bot_app, session=session, chat_id=str(chat_id)):
            return await self._input_show_error(
                chat_id=chat_id,
                ms=ms,
                text=(
                    "Admin выключен для текущей сессии. "
                    "Сначала выполните `/admin enable`."
                ),
            )
        text = str(user_text or "").strip()
        if not text:
            return await self._input_show_error(
                chat_id=chat_id,
                ms=ms,
                text="Пустое сообщение.",
            )

        result = await self._chat_service.send(
            session=session, bot_app=bot_app, text=text,
        )
        reply_text = str(result.get("reply_text") or "") or "(пустой ответ)"
        intent_dict = result.get("intent") if isinstance(result.get("intent"), dict) else None
        intent_type = str((intent_dict or {}).get("type") or "").strip().lower()
        error = result.get("error")

        if error and not intent_dict:
            return await self._input_show_error(
                chat_id=chat_id, ms=ms,
                text=f"Не удалось обработать сообщение: {error}",
            )

        if not intent_dict or intent_type in ("answer", "ask_clarification", "update_memory"):
            await ms.send_plain_text(chat_id, reply_text)
            return ToolResult.ok(reply_text)

        if intent_type == "run_readonly":
            await ms.send_plain_text(chat_id, reply_text)
            target = str((intent_dict or {}).get("target") or "")
            action_id = str((intent_dict or {}).get("action_id") or "")
            if target == "local":
                return await self._input_run_local(
                    bot_app=bot_app, session=session, chat_id=chat_id, ms=ms, action_id=action_id,
                )
            return await self._input_run_ssh(
                bot_app=bot_app, session=session, chat_id=chat_id, ms=ms, action_id=action_id,
            )

        if intent_type in ("propose_action", "propose_new_action", "propose_plan"):
            if bool(result.get("auto_exec")):
                summary = _format_autopilot_exec_summary(
                    intent=intent_dict or {},
                    exec_result=result.get("exec_result") or {},
                )
                await ms.send_plain_text(chat_id, summary)
                return ToolResult.ok(summary)
            approval_id = str(result.get("pending_action_id") or "")
            if not approval_id:
                await ms.send_plain_text(chat_id, reply_text)
                return ToolResult.ok(reply_text)
            blocked = str(result.get("autopilot_blocked") or "").strip()
            message = reply_text
            if blocked:
                message = f"⚠ Autopilot blocked: {blocked}\n\n{reply_text}"
            can_save = bool(
                (intent_dict or {}).get("suggest_save")
                or (intent_dict or {}).get("suggest_save_as_runbook")
            )
            markup = self._build_chat_approval_markup(
                approval_id=approval_id,
                intent_type=intent_type,
                can_save=can_save,
            )
            await ms.send_plain_text(chat_id, message, reply_markup=markup)
            return ToolResult.ok(message)

        await ms.send_plain_text(chat_id, reply_text)
        return ToolResult.ok(reply_text)

    def _build_chat_counters(self, *, session: Any) -> Dict[str, Any]:
        workdir = str(getattr(session, "workdir", "") or "").strip()
        if not workdir:
            return {"messages_count": 0, "pending_count": 0, "last_message_ts": ""}
        try:
            return self._chat_service.counters(workdir)
        except Exception:
            self._log.exception(
                "admin chat: counters failed session_id=%s",
                getattr(session, "id", ""),
            )
            return {"messages_count": 0, "pending_count": 0, "last_message_ts": ""}

    def _build_chat_approval_markup(
        self,
        *,
        approval_id: str,
        intent_type: str,
        can_save: bool,
    ) -> InlineKeyboardMarkup:
        approve_data = build_mode_action_callback_data(
            mode_id=self.mode_id,
            action="chat_approve",
            payload={"id": approval_id},
        )
        reject_data = build_mode_action_callback_data(
            mode_id=self.mode_id,
            action="chat_reject",
            payload={"id": approval_id},
        )
        rows = [
            [
                InlineKeyboardButton("✅ Выполнить", callback_data=approve_data),
                InlineKeyboardButton("❌ Отклонить", callback_data=reject_data),
            ],
        ]
        if can_save and intent_type in ("propose_new_action", "propose_plan"):
            save_data = build_mode_action_callback_data(
                mode_id=self.mode_id,
                action="chat_save_exec",
                payload={"id": approval_id},
            )
            label = "💾 Сохранить и выполнить"
            rows.append([InlineKeyboardButton(label, callback_data=save_data)])
        return InlineKeyboardMarkup(rows)

    async def _input_show_status(
        self,
        *,
        bot_app: Any,
        session: Any,
        chat_id: int,
        ms: Any,
    ) -> ToolResult:
        self._set_session_admin_enabled(
            session=session,
            enabled=self._is_admin_enabled(bot_app=bot_app, session=session, chat_id=str(chat_id)),
        )
        text = self._build_status_text(bot_app=bot_app, session=session)
        await ms.send_text(chat_id, text, md2=True)
        return ToolResult.ok()

    async def _input_show_error(
        self,
        *,
        chat_id: int,
        ms: Any,
        text: str,
    ) -> ToolResult:
        await ms.send_text(chat_id, build_admin_error_text(text), md2=True)
        return ToolResult.fail("admin_input_blocked")

    async def handle_callback(self, callback: CallbackModel, ctx: Dict[str, Any]) -> ToolResult:
        bot_app = ctx.get("bot_app")
        session = ctx.get("session")
        context = ctx.get("context")
        query = ctx.get("query")
        chat_id = self._normalize_callback_chat_id(callback.chat_id)
        if not bot_app or not session:
            return ToolResult.fail("missing_context")

        ms = self._messaging(bot_app=bot_app, context=context)
        action = str(callback.action or "").strip() or "menu"
        payload = dict(getattr(callback, "payload", {}) or {})
        entity_id = str(payload.get("id") or payload.get("value") or "").strip()

        handlers = {
            "menu": lambda: self._cb_menu(bot_app=bot_app, session=session, chat_id=chat_id, context=context, query=query),
            "open": lambda: self._cb_menu(bot_app=bot_app, session=session, chat_id=chat_id, context=context, query=query),
            "show": lambda: self._cb_menu(bot_app=bot_app, session=session, chat_id=chat_id, context=context, query=query),
            "enable": lambda: self._cb_enable(
                bot_app=bot_app,
                session=session,
                chat_id=chat_id,
                user_id=getattr(callback, "user_id", None),
                context=context,
                query=query,
            ),
            "on": lambda: self._cb_enable(
                bot_app=bot_app,
                session=session,
                chat_id=chat_id,
                user_id=getattr(callback, "user_id", None),
                context=context,
                query=query,
            ),
            "disable": lambda: self._cb_disable(
                bot_app=bot_app,
                session=session,
                chat_id=chat_id,
                user_id=getattr(callback, "user_id", None),
                context=context,
                query=query,
            ),
            "off": lambda: self._cb_disable(
                bot_app=bot_app,
                session=session,
                chat_id=chat_id,
                user_id=getattr(callback, "user_id", None),
                context=context,
                query=query,
            ),
            "status": lambda: self._cb_status(bot_app=bot_app, session=session, chat_id=chat_id, query=query, ms=ms),
            "rescan": lambda: self._cb_rescan(
                bot_app=bot_app,
                session=session,
                chat_id=chat_id,
                user_id=getattr(callback, "user_id", None),
                context=context,
                query=query,
            ),
            "incidents": lambda: self._cb_incidents(
                bot_app=bot_app, session=session, chat_id=chat_id, context=context, query=query,
            ),
            "actions_list": lambda: self._cb_actions_list(
                bot_app=bot_app, session=session, chat_id=chat_id, context=context, query=query,
            ),
            "approvals_list": lambda: self._cb_approvals_list(
                bot_app=bot_app, session=session, chat_id=chat_id, context=context, query=query,
            ),
            "skills_list": lambda: self._cb_skills_list(
                bot_app=bot_app, session=session, chat_id=chat_id, context=context, query=query,
            ),
            "runs_list": lambda: self._cb_runs_list(
                bot_app=bot_app, session=session, chat_id=chat_id, context=context, query=query,
            ),
            "ack": lambda: self._cb_ack_incident(
                bot_app=bot_app,
                session=session,
                chat_id=chat_id,
                user_id=getattr(callback, "user_id", None),
                context=context,
                query=query,
                incident_id=entity_id,
            ),
            "revoke": lambda: self._cb_revoke_approval(
                bot_app=bot_app,
                session=session,
                chat_id=chat_id,
                context=context,
                query=query,
                override_id=entity_id,
            ),
            "approvals_clear": lambda: self._cb_clear_approvals(
                bot_app=bot_app, session=session, chat_id=chat_id, context=context, query=query,
            ),
            "skill_approve": lambda: self._cb_approve_skill(
                bot_app=bot_app,
                session=session,
                chat_id=chat_id,
                context=context,
                query=query,
                approval_id=entity_id,
            ),
            "skill_reject": lambda: self._cb_reject_skill(
                bot_app=bot_app,
                session=session,
                chat_id=chat_id,
                context=context,
                query=query,
                approval_id=entity_id,
            ),
            "mute": lambda: self._cb_mute(
                bot_app=bot_app,
                session=session,
                chat_id=chat_id,
                context=context,
                query=query,
                minutes=_to_float_or_default(payload.get("m"), 60.0),
            ),
            "unmute": lambda: self._cb_unmute(
                bot_app=bot_app, session=session, chat_id=chat_id, context=context, query=query,
            ),
            "dryrun_toggle": lambda: self._cb_dryrun_toggle(
                bot_app=bot_app,
                session=session,
                chat_id=chat_id,
                user_id=getattr(callback, "user_id", None),
                context=context,
                query=query,
            ),
            "servers": lambda: self._cb_servers(
                bot_app=bot_app, session=session, chat_id=chat_id, context=context, query=query,
                page=payload.get("p"),
            ),
            "srv": lambda: self._cb_server_detail(
                bot_app=bot_app, session=session, chat_id=chat_id, context=context, query=query,
                server_token=entity_id,
                page=payload.get("p"),
            ),
            "base_view": lambda: self._cb_baseline_view(
                bot_app=bot_app, session=session, chat_id=chat_id, context=context, query=query,
                server_token=entity_id,
            ),
            "base_accept": lambda: self._cb_baseline_accept(
                bot_app=bot_app, session=session, chat_id=chat_id, context=context, query=query,
                server_token=entity_id,
            ),
            "base_discard": lambda: self._cb_baseline_discard(
                bot_app=bot_app, session=session, chat_id=chat_id, context=context, query=query,
                server_token=entity_id,
            ),
            "drifts_view": lambda: self._cb_autonomy_drifts(
                bot_app=bot_app, session=session, chat_id=chat_id, context=context, query=query,
                server_token=entity_id,
            ),
            "drift_ack": lambda: self._cb_drift_ack(
                bot_app=bot_app, session=session, chat_id=chat_id, context=context, query=query,
                user_id=getattr(callback, "user_id", None),
                server_token=entity_id,
                drift_id=str(payload.get("d") or "").strip(),
            ),
            "mem_view": lambda: self._cb_memory_view(
                bot_app=bot_app, session=session, chat_id=chat_id, context=context, query=query,
                server_token=entity_id,
            ),
            "mem_compact": lambda: self._cb_memory_compact(
                bot_app=bot_app, session=session, chat_id=chat_id, context=context, query=query,
                server_token=entity_id,
            ),
            "srv_rescan": lambda: self._cb_server_rescan(
                bot_app=bot_app, session=session, chat_id=chat_id, context=context, query=query,
                server_token=entity_id,
            ),
            "rb_list": lambda: self._cb_runbooks_list(
                bot_app=bot_app, session=session, chat_id=chat_id, context=context, query=query,
                server_token=entity_id,
            ),
            "rb_view": lambda: self._cb_runbook_view(
                bot_app=bot_app, session=session, chat_id=chat_id, context=context, query=query,
                server_token=entity_id,
                runbook_token=str(payload.get("rb") or "").strip(),
            ),
            "rb_validate": lambda: self._cb_runbook_validate(
                bot_app=bot_app, session=session, chat_id=chat_id, context=context, query=query,
                server_token=entity_id,
                runbook_token=str(payload.get("rb") or "").strip(),
            ),
            "rb_promote": lambda: self._cb_runbook_promote(
                bot_app=bot_app, session=session, chat_id=chat_id, context=context, query=query,
                server_token=entity_id,
                runbook_token=str(payload.get("rb") or "").strip(),
            ),
            "autonomy_status": lambda: self._cb_autonomy_status(
                bot_app=bot_app, session=session, chat_id=chat_id, context=context, query=query,
            ),
            "chat_approve": lambda: self._cb_chat_approve(
                bot_app=bot_app,
                session=session,
                chat_id=chat_id,
                user_id=getattr(callback, "user_id", None),
                context=context,
                query=query,
                approval_id=entity_id,
                save_action=False,
            ),
            "chat_save_exec": lambda: self._cb_chat_approve(
                bot_app=bot_app,
                session=session,
                chat_id=chat_id,
                user_id=getattr(callback, "user_id", None),
                context=context,
                query=query,
                approval_id=entity_id,
                save_action=True,
            ),
            "chat_reject": lambda: self._cb_chat_reject(
                bot_app=bot_app,
                session=session,
                chat_id=chat_id,
                context=context,
                query=query,
                approval_id=entity_id,
            ),
        }
        dispatched = await self._dispatch_callback_action(action=action, handlers=handlers)
        if dispatched is not None:
            return dispatched
        return ToolResult.fail("unknown_action")

    async def _cb_menu(
        self,
        *,
        bot_app: Any,
        session: Any,
        chat_id: int,
        context: Any,
        query: Any,
    ) -> ToolResult:
        await self._rerender_menu(bot_app=bot_app, session=session, chat_id=chat_id, context=context, query=query)
        return ToolResult.ok()

    async def _cb_enable(
        self,
        *,
        bot_app: Any,
        session: Any,
        chat_id: int,
        user_id: Any,
        context: Any,
        query: Any,
    ) -> ToolResult:
        ms = self._messaging(bot_app=bot_app, context=context)
        try:
            await self._activate_admin_runtime(
                bot_app=bot_app,
                session=session,
                chat_id=chat_id,
                user_id=user_id,
                context=context,
                ms=ms,
            )
        except Exception:
            self._log.exception(
                "admin cb enable failed session_id=%s", getattr(session, "id", "")
            )
            await ms.send_or_edit(
                query=query,
                chat_id=chat_id,
                text=build_admin_error_text("Не удалось включить Admin режим."),
                md2=True,
            )
            return ToolResult.fail("admin_cb_enable_failed")
        await self._rerender_menu(bot_app=bot_app, session=session, chat_id=chat_id, context=context, query=query, note="Admin включен.")
        return ToolResult.ok()

    async def _cb_disable(
        self,
        *,
        bot_app: Any,
        session: Any,
        chat_id: int,
        user_id: Any,
        context: Any,
        query: Any,
    ) -> ToolResult:
        try:
            await self._deactivate_admin_runtime(
                bot_app=bot_app,
                session=session,
                chat_id=chat_id,
                user_id=user_id,
            )
        except Exception:
            self._log.exception(
                "admin cb disable failed session_id=%s", getattr(session, "id", "")
            )
            ms = self._messaging(bot_app=bot_app, context=context)
            await ms.send_or_edit(
                query=query,
                chat_id=chat_id,
                text=build_admin_error_text("Не удалось выключить Admin режим."),
                md2=True,
            )
            return ToolResult.fail("admin_cb_disable_failed")
        await self._rerender_menu(bot_app=bot_app, session=session, chat_id=chat_id, context=context, query=query, note="Admin выключен.")
        return ToolResult.ok()

    async def _cb_status(
        self,
        *,
        bot_app: Any,
        session: Any,
        chat_id: int,
        query: Any,
        ms: Any,
    ) -> ToolResult:
        text = self._build_status_text(bot_app=bot_app, session=session)
        await ms.send_or_edit(query=query, chat_id=chat_id, text=text, md2=True)
        return ToolResult.ok()

    async def _cb_rescan(
        self,
        *,
        bot_app: Any,
        session: Any,
        chat_id: int,
        user_id: Any,
        context: Any,
        query: Any,
    ) -> ToolResult:
        _ = user_id
        ms = self._messaging(bot_app=bot_app, context=context)
        if is_session_busy(session, getattr(session, "run_lock", None)):
            await ms.send_or_edit(
                query=query,
                chat_id=chat_id,
                text="Сессия занята. Пересканирование окружения доступно только когда сессия свободна.",
                md2=True,
            )
            return ToolResult.fail("admin_rescan_busy")
        try:
            started = await self._start_environment_scan(
                bot_app=bot_app,
                session=session,
                config_payload=self._load_admin_config(bot_app=bot_app, session=session, effective=False),
                chat_id=chat_id,
                context=context,
                force=True,
                initial=False,
            )
        except Exception:
            self._log.exception("admin callback rescan failed session_id=%s", getattr(session, "id", ""))
            await ms.send_or_edit(
                query=query,
                chat_id=chat_id,
                text=build_admin_error_text("Не удалось запустить пересканирование окружения."),
                md2=True,
            )
            return ToolResult.fail("admin_rescan_failed")

        note = "Rescan уже выполняется." if not started else "Rescan окружения запущен."
        await self._rerender_menu(bot_app=bot_app, session=session, chat_id=chat_id, context=context, query=query, note=note)
        return ToolResult.ok()

    # ------------------------------------------------------------------
    # Inline-screen callbacks
    # ------------------------------------------------------------------

    def _short_token(self, value: Any, *, max_len: int = 16) -> str:
        token = str(value or "").strip()
        if not token:
            return ""
        if len(token) <= max_len:
            return token
        digest = hashlib.sha1(token.encode("utf-8")).hexdigest()[:max_len]
        return digest

    def _register_entity_token(self, session: Any, attr: str, *, token: str, value: str) -> None:
        mapping = getattr(session, attr, None)
        if not isinstance(mapping, dict):
            mapping = {}
        mapping[token] = value
        setattr(session, attr, mapping)

    def _resolve_entity_by_token(self, session: Any, attr: str, *, token: str) -> str:
        mapping = getattr(session, attr, None)
        if isinstance(mapping, dict):
            resolved = mapping.get(token)
            if resolved:
                return str(resolved)
        return str(token or "").strip()

    async def _cb_incidents(
        self,
        *,
        bot_app: Any,
        session: Any,
        chat_id: int,
        context: Any,
        query: Any,
    ) -> ToolResult:
        ms = self._messaging(bot_app=bot_app, context=context)
        try:
            store = self._get_state_store(bot_app=bot_app)
            rows = store.list_incidents(
                str(getattr(session, "id", "") or ""),
                chat_id=str(chat_id),
                limit=10,
            )
        except Exception:
            self._log.exception(
                "admin cb incidents list failed session_id=%s", getattr(session, "id", "")
            )
            await ms.send_or_edit(
                query=query,
                chat_id=chat_id,
                text=build_admin_error_text("Не удалось получить список incidents."),
                md2=True,
            )
            return ToolResult.fail("admin_incidents_failed")

        text = build_admin_incidents_screen(incidents=rows)
        rows_kb: list[list[InlineKeyboardButton]] = []
        for item in rows[:8]:
            if not isinstance(item, dict):
                continue
            incident_id = str(item.get("incident_id") or "").strip()
            if not incident_id:
                continue
            token = self._short_token(incident_id, max_len=16)
            self._register_entity_token(session, "_admin_incident_tokens", token=token, value=incident_id)
            rows_kb.append(
                [
                    InlineKeyboardButton(
                        f"✅ Ack {incident_id[:10]}",
                        callback_data=build_mode_action_callback_data(
                            self.mode_id, "ack", session=session, payload={"id": token}
                        ),
                    )
                ]
            )
        rows_kb.append(
            [
                InlineKeyboardButton(
                    "⬅️ Назад",
                    callback_data=build_mode_action_callback_data(self.mode_id, "menu", session=session),
                )
            ]
        )
        await ms.send_or_edit(
            query=query,
            chat_id=chat_id,
            text=text,
            md2=True,
            reply_markup=InlineKeyboardMarkup(rows_kb),
        )
        return ToolResult.ok()

    async def _cb_actions_list(
        self,
        *,
        bot_app: Any,
        session: Any,
        chat_id: int,
        context: Any,
        query: Any,
    ) -> ToolResult:
        ms = self._messaging(bot_app=bot_app, context=context)
        try:
            store = self._get_state_store(bot_app=bot_app)
            rows = store.list_actions(
                str(getattr(session, "id", "") or ""),
                chat_id=str(chat_id),
                limit=10,
            )
        except Exception:
            self._log.exception(
                "admin cb actions list failed session_id=%s", getattr(session, "id", "")
            )
            await ms.send_or_edit(
                query=query,
                chat_id=chat_id,
                text=build_admin_error_text("Не удалось получить список actions."),
                md2=True,
            )
            return ToolResult.fail("admin_actions_failed")

        text = build_admin_actions_screen(actions=rows)
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "⬅️ Назад",
                        callback_data=build_mode_action_callback_data(self.mode_id, "menu", session=session),
                    )
                ]
            ]
        )
        await ms.send_or_edit(query=query, chat_id=chat_id, text=text, md2=True, reply_markup=kb)
        return ToolResult.ok()

    async def _cb_approvals_list(
        self,
        *,
        bot_app: Any,
        session: Any,
        chat_id: int,
        context: Any,
        query: Any,
    ) -> ToolResult:
        ms = self._messaging(bot_app=bot_app, context=context)
        try:
            store = self._get_state_store(bot_app=bot_app)
            rows = store.list_approved_overrides(
                str(getattr(session, "id", "") or ""),
                chat_id=str(chat_id),
                limit=10,
            )
        except Exception:
            self._log.exception(
                "admin cb approvals list failed session_id=%s", getattr(session, "id", "")
            )
            await ms.send_or_edit(
                query=query,
                chat_id=chat_id,
                text=build_admin_error_text("Не удалось получить список approvals."),
                md2=True,
            )
            return ToolResult.fail("admin_approvals_failed")

        text = build_admin_approvals_screen(approvals=rows)
        rows_kb: list[list[InlineKeyboardButton]] = []
        for item in rows[:8]:
            if not isinstance(item, dict):
                continue
            override_id = str(item.get("override_id") or "").strip()
            if not override_id:
                continue
            token = self._short_token(override_id, max_len=16)
            self._register_entity_token(session, "_admin_approval_tokens", token=token, value=override_id)
            rows_kb.append(
                [
                    InlineKeyboardButton(
                        f"❌ Revoke {override_id[:10]}",
                        callback_data=build_mode_action_callback_data(
                            self.mode_id, "revoke", session=session, payload={"id": token}
                        ),
                    )
                ]
            )
        if rows:
            rows_kb.append(
                [
                    InlineKeyboardButton(
                        "🧹 Очистить все",
                        callback_data=build_mode_action_callback_data(self.mode_id, "approvals_clear", session=session),
                    )
                ]
            )
        rows_kb.append(
            [
                InlineKeyboardButton(
                    "⬅️ Назад",
                    callback_data=build_mode_action_callback_data(self.mode_id, "menu", session=session),
                )
            ]
        )
        await ms.send_or_edit(
            query=query,
            chat_id=chat_id,
            text=text,
            md2=True,
            reply_markup=InlineKeyboardMarkup(rows_kb),
        )
        return ToolResult.ok()

    async def _cb_skills_list(
        self,
        *,
        bot_app: Any,
        session: Any,
        chat_id: int,
        context: Any,
        query: Any,
    ) -> ToolResult:
        ms = self._messaging(bot_app=bot_app, context=context)
        skill_runtime = self._resolve_skill_runtime(bot_app=bot_app)
        skills: list[Dict[str, Any]] = []
        if skill_runtime is not None:
            try:
                skills = [
                    self._serialize_pending_skill_install(item)
                    for item in reversed(list(skill_runtime.list_pending_installs(session=session) or []))
                ]
            except Exception:
                self._log.exception(
                    "admin cb skills list failed session_id=%s", getattr(session, "id", "")
                )
        text = build_admin_skills_screen(skills=skills)
        rows_kb: list[list[InlineKeyboardButton]] = []
        for item in skills[:6]:
            approval_id = str(item.get("approval_id") or "").strip()
            if not approval_id:
                continue
            token = self._short_token(approval_id, max_len=16)
            self._register_entity_token(session, "_admin_skill_tokens", token=token, value=approval_id)
            rows_kb.append(
                [
                    InlineKeyboardButton(
                        f"✅ Approve {approval_id[:8]}",
                        callback_data=build_mode_action_callback_data(
                            self.mode_id, "skill_approve", session=session, payload={"id": token}
                        ),
                    ),
                    InlineKeyboardButton(
                        f"❌ Reject {approval_id[:8]}",
                        callback_data=build_mode_action_callback_data(
                            self.mode_id, "skill_reject", session=session, payload={"id": token}
                        ),
                    ),
                ]
            )
        rows_kb.append(
            [
                InlineKeyboardButton(
                    "⬅️ Назад",
                    callback_data=build_mode_action_callback_data(self.mode_id, "menu", session=session),
                )
            ]
        )
        await ms.send_or_edit(
            query=query,
            chat_id=chat_id,
            text=text,
            md2=True,
            reply_markup=InlineKeyboardMarkup(rows_kb),
        )
        return ToolResult.ok()

    async def _cb_runs_list(
        self,
        *,
        bot_app: Any,
        session: Any,
        chat_id: int,
        context: Any,
        query: Any,
    ) -> ToolResult:
        ms = self._messaging(bot_app=bot_app, context=context)
        runs = self._list_mode_runs(session=session, limit=10)
        text = build_admin_runs_screen(runs=runs)
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "⬅️ Назад",
                        callback_data=build_mode_action_callback_data(self.mode_id, "menu", session=session),
                    )
                ]
            ]
        )
        await ms.send_or_edit(query=query, chat_id=chat_id, text=text, md2=True, reply_markup=kb)
        return ToolResult.ok()

    async def _cb_ack_incident(
        self,
        *,
        bot_app: Any,
        session: Any,
        chat_id: int,
        user_id: Any,
        context: Any,
        query: Any,
        incident_id: str,
    ) -> ToolResult:
        ms = self._messaging(bot_app=bot_app, context=context)
        real_id = self._resolve_entity_by_token(session, "_admin_incident_tokens", token=incident_id)
        if not real_id or not is_valid_action_id(real_id):
            await ms.send_or_edit(
                query=query,
                chat_id=chat_id,
                text=build_admin_error_text("Некорректный incident_id."),
                md2=True,
            )
            return ToolResult.fail("admin_ack_invalid_id")
        await self._input_ack_alert(
            bot_app=bot_app,
            session=session,
            chat_id=chat_id,
            user_id=user_id,
            ms=ms,
            incident_id=real_id,
        )
        return await self._cb_incidents(
            bot_app=bot_app, session=session, chat_id=chat_id, context=context, query=query
        )

    async def _cb_revoke_approval(
        self,
        *,
        bot_app: Any,
        session: Any,
        chat_id: int,
        context: Any,
        query: Any,
        override_id: str,
    ) -> ToolResult:
        ms = self._messaging(bot_app=bot_app, context=context)
        real_id = self._resolve_entity_by_token(session, "_admin_approval_tokens", token=override_id)
        if not real_id or not is_valid_action_id(real_id):
            await ms.send_or_edit(
                query=query,
                chat_id=chat_id,
                text=build_admin_error_text("Некорректный override_id."),
                md2=True,
            )
            return ToolResult.fail("admin_revoke_invalid_id")
        await self._input_revoke_approval(
            bot_app=bot_app,
            session=session,
            chat_id=chat_id,
            ms=ms,
            override_id=real_id,
        )
        return await self._cb_approvals_list(
            bot_app=bot_app, session=session, chat_id=chat_id, context=context, query=query
        )

    async def _cb_clear_approvals(
        self,
        *,
        bot_app: Any,
        session: Any,
        chat_id: int,
        context: Any,
        query: Any,
    ) -> ToolResult:
        ms = self._messaging(bot_app=bot_app, context=context)
        await self._input_clear_approvals(
            bot_app=bot_app, session=session, chat_id=chat_id, ms=ms
        )
        return await self._cb_approvals_list(
            bot_app=bot_app, session=session, chat_id=chat_id, context=context, query=query
        )

    async def _cb_chat_approve(
        self,
        *,
        bot_app: Any,
        session: Any,
        chat_id: int,
        user_id: Any,
        context: Any,
        query: Any,
        approval_id: str,
        save_action: bool,
    ) -> ToolResult:
        _ = user_id
        ms = self._messaging(bot_app=bot_app, context=context)
        approval = str(approval_id or "").strip()
        if save_action:
            note = (
                "Сохранение ad-hoc actions как runbook появится в PR-3. "
                "Команда выполнена разово."
            )
            await ms.send_plain_text(chat_id, note)
        result = await self._chat_service.execute_pending(
            session=session, approval_id=approval,
        )
        err = str(result.get("error") or "").strip()
        executed = "exit_code" in result or str(result.get("target_kind") or "") == "plan"
        if not executed:
            if err == "propose_plan_execution_not_implemented":
                await ms.send_plain_text(
                    chat_id,
                    "Plan execution приедет в PR-3. "
                    "Пока план отображён только как предложение.",
                )
                return ToolResult.ok("chat_plan_pending_pr3")
            await ms.send_or_edit(
                query=query, chat_id=chat_id,
                text=build_admin_error_text(err or "execute_pending failed"),
                md2=True,
            )
            return ToolResult.fail(f"admin_chat_{err or 'unknown'}")
        text = _format_chat_result_for_telegram(result)
        await ms.send_plain_text(chat_id, text)
        if result.get("ok"):
            return ToolResult.ok(text)
        return ToolResult.fail("admin_chat_exec_failed")

    async def _cb_chat_reject(
        self,
        *,
        bot_app: Any,
        session: Any,
        chat_id: int,
        context: Any,
        query: Any,
        approval_id: str,
    ) -> ToolResult:
        ms = self._messaging(bot_app=bot_app, context=context)
        workdir = str(getattr(session, "workdir", "") or "").strip()
        result = self._chat_service.reject_pending(
            workdir, approval_id=approval_id,
        )
        if not result.get("ok", False):
            await ms.send_or_edit(
                query=query, chat_id=chat_id,
                text=build_admin_error_text(
                    str(result.get("error") or "Reject failed.")
                ),
                md2=True,
            )
            return ToolResult.fail(f"admin_chat_{result.get('error') or 'reject_failed'}")
        await ms.send_plain_text(chat_id, "Отклонено.")
        return ToolResult.ok("admin_chat_rejected")

    async def _cb_approve_skill(
        self,
        *,
        bot_app: Any,
        session: Any,
        chat_id: int,
        context: Any,
        query: Any,
        approval_id: str,
    ) -> ToolResult:
        ms = self._messaging(bot_app=bot_app, context=context)
        real_id = self._resolve_entity_by_token(session, "_admin_skill_tokens", token=approval_id)
        if not real_id or not is_valid_action_id(real_id):
            await ms.send_or_edit(
                query=query,
                chat_id=chat_id,
                text=build_admin_error_text("Некорректный approval_id."),
                md2=True,
            )
            return ToolResult.fail("admin_skill_approve_invalid_id")
        await self._input_approve_skill_install(
            bot_app=bot_app, session=session, chat_id=chat_id, ms=ms, approval_id=real_id
        )
        return await self._cb_skills_list(
            bot_app=bot_app, session=session, chat_id=chat_id, context=context, query=query
        )

    async def _cb_reject_skill(
        self,
        *,
        bot_app: Any,
        session: Any,
        chat_id: int,
        context: Any,
        query: Any,
        approval_id: str,
    ) -> ToolResult:
        ms = self._messaging(bot_app=bot_app, context=context)
        real_id = self._resolve_entity_by_token(session, "_admin_skill_tokens", token=approval_id)
        if not real_id or not is_valid_action_id(real_id):
            await ms.send_or_edit(
                query=query,
                chat_id=chat_id,
                text=build_admin_error_text("Некорректный approval_id."),
                md2=True,
            )
            return ToolResult.fail("admin_skill_reject_invalid_id")
        await self._input_reject_skill_install(
            bot_app=bot_app, session=session, chat_id=chat_id, ms=ms, approval_id=real_id
        )
        return await self._cb_skills_list(
            bot_app=bot_app, session=session, chat_id=chat_id, context=context, query=query
        )

    async def _cb_mute(
        self,
        *,
        bot_app: Any,
        session: Any,
        chat_id: int,
        context: Any,
        query: Any,
        minutes: float = 60.0,
    ) -> ToolResult:
        ms = self._messaging(bot_app=bot_app, context=context)
        await self._input_mute_alerts(
            bot_app=bot_app,
            session=session,
            chat_id=chat_id,
            ms=ms,
            payload={"minutes": float(minutes)},
        )
        await self._rerender_menu(
            bot_app=bot_app, session=session, chat_id=chat_id, context=context, query=query,
            note=f"Alerts muted на {int(minutes)} мин.",
        )
        return ToolResult.ok()

    async def _cb_unmute(
        self,
        *,
        bot_app: Any,
        session: Any,
        chat_id: int,
        context: Any,
        query: Any,
    ) -> ToolResult:
        ms = self._messaging(bot_app=bot_app, context=context)
        await self._input_unmute_alerts(
            bot_app=bot_app, session=session, chat_id=chat_id, ms=ms
        )
        await self._rerender_menu(
            bot_app=bot_app, session=session, chat_id=chat_id, context=context, query=query,
            note="Alerts unmuted.",
        )
        return ToolResult.ok()

    async def _cb_dryrun_toggle(
        self,
        *,
        bot_app: Any,
        session: Any,
        chat_id: int,
        user_id: Any,
        context: Any,
        query: Any,
    ) -> ToolResult:
        ms = self._messaging(bot_app=bot_app, context=context)
        state = self._get_admin_session_state(bot_app=bot_app, session=session, chat_id=chat_id)
        current_dry_run = bool(state.get("dry_run", True))
        new_dry_run = not current_dry_run
        await self._input_set_dry_run(
            bot_app=bot_app,
            session=session,
            chat_id=chat_id,
            user_id=user_id,
            ms=ms,
            payload={"dry_run": new_dry_run},
        )
        await self._rerender_menu(
            bot_app=bot_app, session=session, chat_id=chat_id, context=context, query=query,
            note=f"Dry-run: {'on' if new_dry_run else 'off'}",
        )
        return ToolResult.ok()

    def _list_mode_runs(self, *, session: Any, limit: int = 10) -> list[Dict[str, Any]]:
        artifact_store = self._artifact_store()
        if artifact_store is None:
            return []
        try:
            list_runs = getattr(artifact_store, "list_runs", None)
            if callable(list_runs):
                rows = list_runs(session=session, mode_id=self.mode_id, limit=int(max(1, limit)))
            else:
                rows = []
        except Exception:
            self._log.exception(
                "admin list_runs failed session_id=%s", getattr(session, "id", "")
            )
            return []
        result: list[Dict[str, Any]] = []
        for entry in rows or []:
            row: Dict[str, Any] = {}
            if isinstance(entry, dict):
                row = dict(entry)
            else:
                for key in ("run_id", "status", "phase", "started_at", "finished_at", "created_at"):
                    value = getattr(entry, key, None)
                    if value is not None:
                        row[key] = value
            if not row:
                continue
            state = row.get("state")
            if isinstance(state, dict):
                row.setdefault("status", state.get("status"))
                row.setdefault("phase", state.get("phase"))
            result.append(row)
        return result

    def _status_payload(self, *, bot_app: Any, session: Any, chat_id: Any = None) -> Dict[str, Any]:
        run_lock = getattr(session, "run_lock", None)
        tick_fn = getattr(session, "is_active_by_tick", None)
        tick_active = bool(tick_fn()) if callable(tick_fn) else False
        try:
            resolved_chat_id = int(chat_id if chat_id is not None else getattr(session, "chat_id", 0) or 0)
        except Exception:
            resolved_chat_id = 0
        state = self._get_admin_session_state(bot_app=bot_app, session=session, chat_id=resolved_chat_id)
        mode_tasks_running = bool(self._mode_task_names(bot_app=bot_app, session=session))
        raw_config = self._load_admin_config(bot_app=bot_app, session=session, effective=False)
        effective_config = self._load_admin_config(bot_app=bot_app, session=session, effective=True)
        admin_cfg = raw_config.get("admin", {}) if isinstance(raw_config, dict) else {}
        effective_admin_cfg = effective_config.get("admin", {}) if isinstance(effective_config, dict) else {}
        runtime_cfg = admin_cfg.get("runtime", {}) if isinstance(admin_cfg, dict) else {}
        environment_cfg = effective_admin_cfg.get("environment", {}) if isinstance(effective_admin_cfg, dict) else {}
        services_cfg = environment_cfg.get("services", {}) if isinstance(environment_cfg, dict) else {}
        scan_ready = bool(
            isinstance(runtime_cfg, dict)
            and str(runtime_cfg.get("scan_status") or "not_started").strip().lower() == "ready"
        )
        runtime_status = self._derive_runtime_status_payload(
            session=session,
            active=bool(state.get("enabled", False)),
            mode_tasks_running=mode_tasks_running,
        )
        runner = self._resolve_runner_service()
        component_readiness = {
            "monitor": bool(scan_ready and self._has_monitor_servers(effective_config)),
            "analyzer": bool(scan_ready and runner is not None),
            "executor": bool(scan_ready and runner is not None),
            "notifier": bool(scan_ready and runner is not None and getattr(runner, "_notifier", None) is not None),
        }
        pending_ask_user = self._resolve_pending_ask_user(
            bot_app,
            session_id=str(getattr(session, "id", "") or ""),
            chat_id=resolved_chat_id,
            session=session,
        )
        try:
            store = self._get_state_store(bot_app=bot_app)
            recent_incidents = self._summarize_state_rows(
                store.list_incidents(str(getattr(session, "id", "") or ""), chat_id=resolved_chat_id, limit=5),
                id_key="incident_id",
            )
            recent_actions = self._summarize_state_rows(
                store.list_actions(str(getattr(session, "id", "") or ""), chat_id=resolved_chat_id, limit=5),
                id_key="action_id",
            )
            approved_overrides = self._summarize_state_rows(
                store.list_approved_overrides(str(getattr(session, "id", "") or ""), chat_id=resolved_chat_id, limit=5),
                id_key="override_id",
            )
        except Exception:
            self._log.exception("admin status failed to load state rows session_id=%s", getattr(session, "id", ""))
            recent_incidents = []
            recent_actions = []
            approved_overrides = []
        pending_skill_installs: Dict[str, Any] = {
            "count": 0,
            "active": False,
            "items": [],
        }
        skill_runtime = self._resolve_skill_runtime(bot_app=bot_app)
        if skill_runtime is not None:
            try:
                skill_records = list(reversed(list(skill_runtime.list_pending_installs(session=session) or [])))
                pending_skill_installs = {
                    "count": int(len(skill_records)),
                    "active": bool(skill_records),
                    "items": [self._serialize_pending_skill_install(item) for item in skill_records[:5]],
                }
            except Exception:
                self._log.exception(
                    "admin status failed to load pending skill installs session_id=%s",
                    getattr(session, "id", ""),
                )
        payload: Dict[str, Any] = {
            "mode": self.mode_id,
            "session_id": str(getattr(session, "id", "") or ""),
            "active": bool(state.get("enabled", False)),
            "busy": bool(getattr(session, "busy", False)),
            "run_lock_locked": bool(run_lock and run_lock.locked()),
            "tick_active": tick_active,
            "mode_tasks_running": mode_tasks_running,
            "pinned_cli": dict(runtime_cfg.get("pinned_cli") or {}) if isinstance(runtime_cfg, dict) else {},
            "pinned_executor_profile": (
                str(runtime_cfg.get("pinned_executor_profile") or "").strip() or None
                if isinstance(runtime_cfg, dict)
                else None
            ),
            "initialized_at": runtime_cfg.get("initialized_at") if isinstance(runtime_cfg, dict) else None,
            "last_scan_at": runtime_cfg.get("last_scan_at") if isinstance(runtime_cfg, dict) else None,
            "scan_status": str(runtime_cfg.get("scan_status") or "not_started").strip().lower()
            if isinstance(runtime_cfg, dict)
            else "not_started",
            "scan_error": runtime_cfg.get("scan_error") if isinstance(runtime_cfg, dict) else None,
            "component_readiness": component_readiness,
            "environment_services": sorted(services_cfg.keys()) if isinstance(services_cfg, dict) else [],
            "environment_stack_facts": dict(environment_cfg.get("stack_facts") or {})
            if isinstance(environment_cfg, dict)
            else {},
            "pending_ask_user": pending_ask_user,
            "pending_approvals": {
                "count": int(pending_ask_user.get("count") or 0),
                "active": bool(pending_ask_user.get("active", False)),
            },
            "pending_skill_installs": pending_skill_installs,
            "mute_state": {
                "muted_until_ts": state.get("muted_until_ts"),
                "muted": bool(state.get("muted_until_ts") and float(state.get("muted_until_ts") or 0) > time.time()),
            },
            "recent_incidents": recent_incidents,
            "recent_admin_actions": recent_actions,
            "approved_overrides": approved_overrides,
            "chat": self._build_chat_counters(session=session),
        }
        payload.update(runtime_status)
        last_snapshot = payload.get("last_monitor_snapshot")
        if isinstance(last_snapshot, Mapping):
            payload["last_monitor_snapshot"] = self._summarize_runtime_snapshot(last_snapshot)
        last_action = payload.get("last_action_result")
        if isinstance(last_action, Mapping):
            payload["last_action"] = dict(last_action)
        elif recent_actions:
            payload["last_action"] = dict(recent_actions[0])
        if not payload.get("last_analyzer_decision") and recent_actions:
            latest_payload = recent_actions[0].get("payload")
            if isinstance(latest_payload, Mapping):
                decision = latest_payload.get("decision")
                if isinstance(decision, Mapping):
                    payload["last_analyzer_decision"] = dict(decision)
        payload.setdefault("last_monitor_snapshot", {})
        payload.setdefault("last_analyzer_decision", {})
        payload.setdefault("last_action", {})
        validate_admin_payload(payload, AdminStatusPayloadSchema, contract="status_payload")
        return payload

    def _resolve_skill_runtime(self, *, bot_app: Any) -> Any | None:
        runtime = self._optional_skill_runtime()
        if runtime is not None:
            return runtime
        return getattr(bot_app, "mode_skill_runtime", None)

    @staticmethod
    def _serialize_pending_skill_install(record: Any) -> Dict[str, Any]:
        requester = dict(getattr(record, "requester", {}) or {})
        return {
            "approval_id": str(getattr(record, "approval_id", "") or ""),
            "skill_id": str(getattr(record, "skill_id", "") or ""),
            "mode_id": str(getattr(record, "mode_id", "") or ""),
            "phase": str(getattr(record, "phase", "") or ""),
            "source": str(getattr(record, "source", "") or ""),
            "acquisition_source": str(getattr(record, "acquisition_source", "") or ""),
            "ref": str(getattr(record, "ref", "") or ""),
            "created_at": getattr(record, "created_at", None),
            "requester": requester,
            "requester_actor_chat_id": str(requester.get("actor_chat_id") or "").strip() or None,
        }

    def _build_status_text(self, *, bot_app: Any, session: Any) -> str:
        try:
            payload = self._status_payload(bot_app=bot_app, session=session)
            return build_admin_status_text(payload)
        except Exception:
            self._log.exception("admin status payload build failed")
            return build_admin_error_text("Не удалось собрать статус режима")

    # ------------------------------------------------------------------
    # Autonomy (inventory/baseline/drift/memory/runbooks) callbacks
    # ------------------------------------------------------------------

    def _autonomy_service(self, *, session: Any) -> Optional[AdminAutonomyService]:
        workdir = str(getattr(session, "workdir", "") or "").strip()
        if not workdir:
            return None
        try:
            return AdminAutonomyService(workdir)
        except Exception:
            self._log.exception("admin autonomy: failed to init service")
            return None

    def _resolve_server_id_from_token(self, session: Any, *, token: str) -> str:
        return self._resolve_entity_by_token(session, "_admin_server_tokens", token=token)

    def _back_to_servers_button(self, session: Any, *, page: int = 0) -> InlineKeyboardButton:
        payload = {"p": str(page)} if page > 0 else None
        return InlineKeyboardButton(
            "⬅️ К серверам",
            callback_data=build_mode_action_callback_data(
                self.mode_id, "servers", session=session, payload=payload,
            ),
        )

    def _back_to_server_detail_button(self, session: Any, *, server_token: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(
            "⬅️ К серверу",
            callback_data=build_mode_action_callback_data(
                self.mode_id, "srv", session=session, payload={"id": server_token},
            ),
        )

    async def _send_autonomy_error(
        self,
        *,
        ms: Any,
        query: Any,
        chat_id: int,
        message: str,
    ) -> ToolResult:
        await ms.send_or_edit(
            query=query,
            chat_id=chat_id,
            text=build_admin_error_text(message),
            md2=True,
        )
        return ToolResult.fail("admin_autonomy_failed")

    async def _cb_servers(
        self,
        *,
        bot_app: Any,
        session: Any,
        chat_id: int,
        context: Any,
        query: Any,
        page: Any = 0,
    ) -> ToolResult:
        ms = self._messaging(bot_app=bot_app, context=context)
        svc = self._autonomy_service(session=session)
        if svc is None:
            return await self._send_autonomy_error(
                ms=ms, query=query, chat_id=chat_id,
                message="Workdir сессии не задан — autonomy недоступна.",
            )
        try:
            summaries = svc.list_servers()
            totals = svc.global_summary()
        except Exception:
            self._log.exception("admin autonomy: list_servers failed")
            return await self._send_autonomy_error(
                ms=ms, query=query, chat_id=chat_id,
                message="Не удалось получить список серверов.",
            )
        try:
            page_index = int(page)
        except (TypeError, ValueError):
            page_index = 0
        page_index = max(0, page_index)
        summaries_payload = [s.to_dict() for s in summaries]
        text = build_admin_servers_screen(servers=summaries_payload, totals=totals)
        page_size = 8
        page_count = max(1, (len(summaries) + page_size - 1) // page_size)
        page_index = min(page_index, page_count - 1)
        page_start = page_index * page_size
        page_summaries = summaries[page_start:page_start + page_size]
        rows_kb: list[list[InlineKeyboardButton]] = []
        for s in page_summaries:
            sid = s.server_id
            token = self._short_token(sid, max_len=16)
            self._register_entity_token(session, "_admin_server_tokens", token=token, value=sid)
            icon = {"alarm": "🔴", "warn": "🟡", "proposed_baseline": "📋",
                    "no_baseline": "🆕", "ok": "🟢"}.get(s.status(), "•")
            label = (s.label or sid)[:22]
            button_payload = {"id": token}
            if page_index > 0:
                button_payload["p"] = str(page_index)
            rows_kb.append([
                InlineKeyboardButton(
                    f"{icon} {label}",
                    callback_data=build_mode_action_callback_data(
                        self.mode_id, "srv", session=session, payload=button_payload,
                    ),
                )
            ])
        if page_count > 1:
            nav_row: list[InlineKeyboardButton] = []
            if page_index > 0:
                nav_row.append(
                    InlineKeyboardButton(
                        "◀️",
                        callback_data=build_mode_action_callback_data(
                            self.mode_id, "servers", session=session, payload={"p": str(page_index - 1)},
                        ),
                    )
                )
            nav_row.append(
                InlineKeyboardButton(
                    f"{page_index + 1}/{page_count}",
                    callback_data=build_mode_action_callback_data(
                        self.mode_id, "servers", session=session, payload={"p": str(page_index)},
                    ),
                )
            )
            if page_index + 1 < page_count:
                nav_row.append(
                    InlineKeyboardButton(
                        "▶️",
                        callback_data=build_mode_action_callback_data(
                            self.mode_id, "servers", session=session, payload={"p": str(page_index + 1)},
                        ),
                    )
                )
            rows_kb.append(nav_row)
        rows_kb.append([
            InlineKeyboardButton(
                "⬅️ Назад",
                callback_data=build_mode_action_callback_data(self.mode_id, "menu", session=session),
            )
        ])
        await ms.send_or_edit(
            query=query, chat_id=chat_id, text=text, md2=True,
            reply_markup=InlineKeyboardMarkup(rows_kb),
        )
        return ToolResult.ok()

    async def _cb_server_detail(
        self,
        *,
        bot_app: Any,
        session: Any,
        chat_id: int,
        context: Any,
        query: Any,
        server_token: str,
        page: Any = 0,
    ) -> ToolResult:
        ms = self._messaging(bot_app=bot_app, context=context)
        svc = self._autonomy_service(session=session)
        sid = self._resolve_server_id_from_token(session, token=server_token)
        if svc is None or not sid:
            return await self._send_autonomy_error(
                ms=ms, query=query, chat_id=chat_id, message="Сервер не найден.",
            )
        try:
            summary = svc.get_server_summary(sid)
        except Exception:
            self._log.exception("admin autonomy: get_server_summary failed sid=%s", sid)
            return await self._send_autonomy_error(
                ms=ms, query=query, chat_id=chat_id, message="Не удалось получить данные по серверу.",
            )
        if summary is None:
            return await self._send_autonomy_error(
                ms=ms, query=query, chat_id=chat_id, message=f"Сервер {sid} не найден.",
            )
        try:
            page_index = max(0, int(page))
        except (TypeError, ValueError):
            page_index = 0
        text = build_admin_server_detail_screen(summary=summary.to_dict())
        payload = {"id": server_token}
        if page_index > 0:
            payload["p"] = str(page_index)
        rows_kb = [
            [
                InlineKeyboardButton(
                    "📋 Baseline",
                    callback_data=build_mode_action_callback_data(
                        self.mode_id, "base_view", session=session, payload=payload,
                    ),
                ),
                InlineKeyboardButton(
                    "🚨 Drifts",
                    callback_data=build_mode_action_callback_data(
                        self.mode_id, "drifts_view", session=session, payload=payload,
                    ),
                ),
            ],
            [
                InlineKeyboardButton(
                    "🧠 Memory",
                    callback_data=build_mode_action_callback_data(
                        self.mode_id, "mem_view", session=session, payload=payload,
                    ),
                ),
                InlineKeyboardButton(
                    "📚 Runbooks",
                    callback_data=build_mode_action_callback_data(
                        self.mode_id, "rb_list", session=session, payload=payload,
                    ),
                ),
            ],
            [
                InlineKeyboardButton(
                    "🔄 Rescan",
                    callback_data=build_mode_action_callback_data(
                        self.mode_id, "srv_rescan", session=session, payload=payload,
                    ),
                ),
                self._back_to_servers_button(session, page=page_index),
            ],
        ]
        await ms.send_or_edit(
            query=query, chat_id=chat_id, text=text, md2=True,
            reply_markup=InlineKeyboardMarkup(rows_kb),
        )
        return ToolResult.ok()

    async def _cb_baseline_view(
        self,
        *,
        bot_app: Any,
        session: Any,
        chat_id: int,
        context: Any,
        query: Any,
        server_token: str,
    ) -> ToolResult:
        ms = self._messaging(bot_app=bot_app, context=context)
        svc = self._autonomy_service(session=session)
        sid = self._resolve_server_id_from_token(session, token=server_token)
        if svc is None or not sid:
            return await self._send_autonomy_error(
                ms=ms, query=query, chat_id=chat_id, message="Сервер не найден.",
            )
        try:
            info = svc.get_baseline(sid)
        except Exception:
            self._log.exception("admin autonomy: get_baseline failed sid=%s", sid)
            return await self._send_autonomy_error(
                ms=ms, query=query, chat_id=chat_id, message="Не удалось прочитать baseline.",
            )
        text = build_admin_baseline_screen(server_id=sid, info=info)
        payload = {"id": server_token}
        kb_rows: list[list[InlineKeyboardButton]] = []
        if info.get("has_proposed"):
            kb_rows.append([
                InlineKeyboardButton(
                    "✅ Accept proposed",
                    callback_data=build_mode_action_callback_data(
                        self.mode_id, "base_accept", session=session, payload=payload,
                    ),
                ),
                InlineKeyboardButton(
                    "🗑 Discard proposed",
                    callback_data=build_mode_action_callback_data(
                        self.mode_id, "base_discard", session=session, payload=payload,
                    ),
                ),
            ])
        kb_rows.append([self._back_to_server_detail_button(session, server_token=server_token)])
        await ms.send_or_edit(
            query=query, chat_id=chat_id, text=text, md2=True,
            reply_markup=InlineKeyboardMarkup(kb_rows),
        )
        return ToolResult.ok()

    async def _cb_baseline_accept(
        self,
        *,
        bot_app: Any,
        session: Any,
        chat_id: int,
        context: Any,
        query: Any,
        server_token: str,
    ) -> ToolResult:
        ms = self._messaging(bot_app=bot_app, context=context)
        svc = self._autonomy_service(session=session)
        sid = self._resolve_server_id_from_token(session, token=server_token)
        if svc is None or not sid:
            return await self._send_autonomy_error(
                ms=ms, query=query, chat_id=chat_id, message="Сервер не найден.",
            )
        try:
            svc.accept_baseline(sid)
        except Exception as exc:
            self._log.exception("admin autonomy: accept_baseline failed sid=%s", sid)
            return await self._send_autonomy_error(
                ms=ms, query=query, chat_id=chat_id,
                message=f"Accept baseline не удался: {exc}",
            )
        return await self._cb_baseline_view(
            bot_app=bot_app, session=session, chat_id=chat_id,
            context=context, query=query, server_token=server_token,
        )

    async def _cb_baseline_discard(
        self,
        *,
        bot_app: Any,
        session: Any,
        chat_id: int,
        context: Any,
        query: Any,
        server_token: str,
    ) -> ToolResult:
        ms = self._messaging(bot_app=bot_app, context=context)
        svc = self._autonomy_service(session=session)
        sid = self._resolve_server_id_from_token(session, token=server_token)
        if svc is None or not sid:
            return await self._send_autonomy_error(
                ms=ms, query=query, chat_id=chat_id, message="Сервер не найден.",
            )
        try:
            svc.discard_baseline_proposal(sid)
        except Exception as exc:
            self._log.exception("admin autonomy: discard_baseline failed sid=%s", sid)
            return await self._send_autonomy_error(
                ms=ms, query=query, chat_id=chat_id,
                message=f"Discard не удался: {exc}",
            )
        return await self._cb_baseline_view(
            bot_app=bot_app, session=session, chat_id=chat_id,
            context=context, query=query, server_token=server_token,
        )

    async def _cb_autonomy_status(
        self,
        *,
        bot_app: Any,
        session: Any,
        chat_id: int,
        context: Any,
        query: Any,
    ) -> ToolResult:
        ms = self._messaging(bot_app=bot_app, context=context)
        svc = self._autonomy_service(session=session)
        if svc is None:
            return await self._send_autonomy_error(
                ms=ms, query=query, chat_id=chat_id, message="Autonomy service недоступен.",
            )
        try:
            status = svc.autonomy_status()
        except Exception:
            self._log.exception("admin autonomy: status failed")
            return await self._send_autonomy_error(
                ms=ms, query=query, chat_id=chat_id, message="Не удалось получить статус автономии.",
            )
        text = build_admin_autonomy_status_screen(status)
        rows = [
            [
                InlineKeyboardButton(
                    "🖥 Серверы",
                    callback_data=build_mode_action_callback_data(self.mode_id, "servers", session=session),
                ),
                InlineKeyboardButton(
                    "⬅️ В меню",
                    callback_data=build_mode_action_callback_data(self.mode_id, "menu", session=session),
                ),
            ],
        ]
        await ms.send_or_edit(
            query=query, chat_id=chat_id, text=text, md2=True,
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return ToolResult.ok()

    async def _cb_autonomy_drifts(
        self,
        *,
        bot_app: Any,
        session: Any,
        chat_id: int,
        context: Any,
        query: Any,
        server_token: str,
    ) -> ToolResult:
        ms = self._messaging(bot_app=bot_app, context=context)
        svc = self._autonomy_service(session=session)
        sid = self._resolve_server_id_from_token(session, token=server_token)
        if svc is None or not sid:
            return await self._send_autonomy_error(
                ms=ms, query=query, chat_id=chat_id, message="Сервер не найден.",
            )
        try:
            drifts = svc.list_drifts(sid, limit=15, open_only=True)
        except Exception:
            self._log.exception("admin autonomy: list_drifts failed sid=%s", sid)
            return await self._send_autonomy_error(
                ms=ms, query=query, chat_id=chat_id,
                message="Не удалось получить список drift-ов.",
            )
        text = build_admin_autonomy_drifts_screen(server_id=sid, drifts=drifts)
        kb_rows: list[list[InlineKeyboardButton]] = []
        for drift in drifts[:8]:
            did = drift.get("id")
            if did is None:
                continue
            kb_rows.append([
                InlineKeyboardButton(
                    f"✅ Ack #{did}",
                    callback_data=build_mode_action_callback_data(
                        self.mode_id, "drift_ack", session=session,
                        payload={"id": server_token, "d": str(did)},
                    ),
                )
            ])
        kb_rows.append([self._back_to_server_detail_button(session, server_token=server_token)])
        await ms.send_or_edit(
            query=query, chat_id=chat_id, text=text, md2=True,
            reply_markup=InlineKeyboardMarkup(kb_rows),
        )
        return ToolResult.ok()

    async def _cb_drift_ack(
        self,
        *,
        bot_app: Any,
        session: Any,
        chat_id: int,
        context: Any,
        query: Any,
        user_id: Any,
        server_token: str,
        drift_id: str,
    ) -> ToolResult:
        ms = self._messaging(bot_app=bot_app, context=context)
        svc = self._autonomy_service(session=session)
        sid = self._resolve_server_id_from_token(session, token=server_token)
        if svc is None or not sid or not drift_id:
            return await self._send_autonomy_error(
                ms=ms, query=query, chat_id=chat_id, message="Drift не найден.",
            )
        try:
            svc.ack_drift(sid, int(drift_id), by=str(user_id or "tg"))
        except (ValueError, TypeError):
            return await self._send_autonomy_error(
                ms=ms, query=query, chat_id=chat_id, message="Некорректный drift_id.",
            )
        except Exception:
            self._log.exception("admin autonomy: ack_drift failed sid=%s drift=%s", sid, drift_id)
            return await self._send_autonomy_error(
                ms=ms, query=query, chat_id=chat_id, message="Не удалось подтвердить drift.",
            )
        return await self._cb_autonomy_drifts(
            bot_app=bot_app, session=session, chat_id=chat_id,
            context=context, query=query, server_token=server_token,
        )

    async def _cb_memory_view(
        self,
        *,
        bot_app: Any,
        session: Any,
        chat_id: int,
        context: Any,
        query: Any,
        server_token: str,
    ) -> ToolResult:
        ms = self._messaging(bot_app=bot_app, context=context)
        svc = self._autonomy_service(session=session)
        sid = self._resolve_server_id_from_token(session, token=server_token)
        if svc is None or not sid:
            return await self._send_autonomy_error(
                ms=ms, query=query, chat_id=chat_id, message="Сервер не найден.",
            )
        try:
            memory = svc.get_memory(sid)
        except Exception:
            self._log.exception("admin autonomy: get_memory failed sid=%s", sid)
            return await self._send_autonomy_error(
                ms=ms, query=query, chat_id=chat_id, message="Не удалось получить memory.",
            )
        text = build_admin_memory_screen(server_id=sid, memory=memory)
        payload = {"id": server_token}
        kb_rows: list[list[InlineKeyboardButton]] = [
            [
                InlineKeyboardButton(
                    "🗜 Compact",
                    callback_data=build_mode_action_callback_data(
                        self.mode_id, "mem_compact", session=session, payload=payload,
                    ),
                ),
                self._back_to_server_detail_button(session, server_token=server_token),
            ]
        ]
        await ms.send_or_edit(
            query=query, chat_id=chat_id, text=text, md2=True,
            reply_markup=InlineKeyboardMarkup(kb_rows),
        )
        return ToolResult.ok()

    async def _cb_memory_compact(
        self,
        *,
        bot_app: Any,
        session: Any,
        chat_id: int,
        context: Any,
        query: Any,
        server_token: str,
    ) -> ToolResult:
        ms = self._messaging(bot_app=bot_app, context=context)
        svc = self._autonomy_service(session=session)
        sid = self._resolve_server_id_from_token(session, token=server_token)
        if svc is None or not sid:
            return await self._send_autonomy_error(
                ms=ms, query=query, chat_id=chat_id, message="Сервер не найден.",
            )
        try:
            svc.compact_memory(sid, force=True)
        except Exception:
            self._log.exception("admin autonomy: compact_memory failed sid=%s", sid)
            return await self._send_autonomy_error(
                ms=ms, query=query, chat_id=chat_id, message="Compact не удался.",
            )
        return await self._cb_memory_view(
            bot_app=bot_app, session=session, chat_id=chat_id,
            context=context, query=query, server_token=server_token,
        )

    async def _cb_server_rescan(
        self,
        *,
        bot_app: Any,
        session: Any,
        chat_id: int,
        context: Any,
        query: Any,
        server_token: str,
    ) -> ToolResult:
        ms = self._messaging(bot_app=bot_app, context=context)
        svc = self._autonomy_service(session=session)
        sid = self._resolve_server_id_from_token(session, token=server_token)
        if svc is None or not sid:
            return await self._send_autonomy_error(
                ms=ms, query=query, chat_id=chat_id, message="Сервер не найден.",
            )
        try:
            report = await svc.rescan_server(sid)
        except Exception as exc:
            self._log.exception("admin autonomy: rescan_server failed sid=%s", sid)
            return await self._send_autonomy_error(
                ms=ms, query=query, chat_id=chat_id,
                message=f"Rescan не удался: {exc}",
            )
        report_payload = {
            "ok": report.ok,
            "baseline_present": report.baseline_present,
            "snapshots_written": report.snapshots_written,
            "drifts_written": report.drifts_written,
            "drifts_by_severity": dict(report.drifts_by_severity or {}),
            "error": report.error,
        }
        text = build_admin_rescan_report_screen(server_id=sid, report=report_payload)
        kb_rows = [[
            self._back_to_server_detail_button(session, server_token=server_token),
        ]]
        await ms.send_or_edit(
            query=query, chat_id=chat_id, text=text, md2=True,
            reply_markup=InlineKeyboardMarkup(kb_rows),
        )
        return ToolResult.ok()

    async def _cb_runbooks_list(
        self,
        *,
        bot_app: Any,
        session: Any,
        chat_id: int,
        context: Any,
        query: Any,
        server_token: str,
    ) -> ToolResult:
        return await self._runbook.cb_runbooks_list(
            bot_app=bot_app, session=session, chat_id=chat_id,
            context=context, query=query, server_token=server_token,
        )

    async def _cb_runbook_view(
        self,
        *,
        bot_app: Any,
        session: Any,
        chat_id: int,
        context: Any,
        query: Any,
        server_token: str,
        runbook_token: str,
    ) -> ToolResult:
        return await self._runbook.cb_runbook_view(
            bot_app=bot_app, session=session, chat_id=chat_id,
            context=context, query=query, server_token=server_token,
            runbook_token=runbook_token,
        )

    async def _cb_runbook_validate(
        self,
        *,
        bot_app: Any,
        session: Any,
        chat_id: int,
        context: Any,
        query: Any,
        server_token: str,
        runbook_token: str,
    ) -> ToolResult:
        return await self._runbook.cb_runbook_validate(
            bot_app=bot_app, session=session, chat_id=chat_id,
            context=context, query=query, server_token=server_token,
            runbook_token=runbook_token,
        )

    async def _cb_runbook_promote(
        self,
        *,
        bot_app: Any,
        session: Any,
        chat_id: int,
        context: Any,
        query: Any,
        server_token: str,
        runbook_token: str,
    ) -> ToolResult:
        return await self._runbook.cb_runbook_promote(
            bot_app=bot_app, session=session, chat_id=chat_id,
            context=context, query=query, server_token=server_token,
            runbook_token=runbook_token,
        )

    async def _rerender_menu(
        self,
        *,
        bot_app: Any,
        session: Any,
        chat_id: int,
        context: Any,
        query: Any,
        note: str = "",
    ) -> None:
        self._set_session_admin_enabled(
            session=session,
            enabled=self._is_admin_enabled(bot_app=bot_app, session=session, chat_id=str(chat_id)),
        )
        text, keyboard = self.build_menu(
            session,
            back_callback="sess_active",
            back_text="⬅️ Назад",
        )
        if note:
            text = merge_menu_with_note(menu_text=text, note=note)
        ms = self._messaging(bot_app=bot_app, context=context)
        await ms.send_or_edit(query=query, chat_id=chat_id, text=text, md2=True, reply_markup=keyboard)

    def build_menu(
        self,
        session: Any,
        back_callback: str = "sess_active",
        back_text: str = "⬅️ Назад",
    ) -> tuple[str, Any]:
        active = bool(getattr(session, "admin_enabled", False))
        muted = self._is_session_muted(session)
        text = build_admin_menu_text(
            session_id=str(getattr(session, "id", "") or "-"),
            active=active,
        )
        rows: list[list[InlineKeyboardButton]] = []
        if active:
            rows.append(
                [
                    InlineKeyboardButton(
                        "🔴 Выключить Admin",
                        callback_data=build_mode_action_callback_data(self.mode_id, "disable", session=session),
                    )
                ]
            )
        else:
            rows.append(
                [
                    InlineKeyboardButton(
                        "🟢 Включить Admin",
                        callback_data=build_mode_action_callback_data(self.mode_id, "enable", session=session),
                    )
                ]
            )
        rows.append(
            [
                InlineKeyboardButton(
                    "📊 Статус",
                    callback_data=build_mode_action_callback_data(self.mode_id, "status", session=session),
                ),
                InlineKeyboardButton(
                    "🔄 Rescan",
                    callback_data=build_mode_action_callback_data(self.mode_id, "rescan", session=session),
                ),
            ]
        )
        if active:
            rows.append(
                [
                    InlineKeyboardButton(
                        "🚨 Incidents",
                        callback_data=build_mode_action_callback_data(self.mode_id, "incidents", session=session),
                    ),
                    InlineKeyboardButton(
                        "⚙️ Actions",
                        callback_data=build_mode_action_callback_data(self.mode_id, "actions_list", session=session),
                    ),
                ]
            )
            rows.append(
                [
                    InlineKeyboardButton(
                        "✅ Approvals",
                        callback_data=build_mode_action_callback_data(self.mode_id, "approvals_list", session=session),
                    ),
                    InlineKeyboardButton(
                        "🧩 Skills",
                        callback_data=build_mode_action_callback_data(self.mode_id, "skills_list", session=session),
                    ),
                ]
            )
            rows.append(
                [
                    InlineKeyboardButton(
                        "📜 Runs",
                        callback_data=build_mode_action_callback_data(self.mode_id, "runs_list", session=session),
                    ),
                    InlineKeyboardButton(
                        "🔕 Mute" if not muted else "🔔 Unmute",
                        callback_data=build_mode_action_callback_data(
                            self.mode_id,
                            "mute" if not muted else "unmute",
                            session=session,
                        ),
                    ),
                ]
            )
            rows.append(
                [
                    InlineKeyboardButton(
                        "🧪 Dry-run toggle",
                        callback_data=build_mode_action_callback_data(self.mode_id, "dryrun_toggle", session=session),
                    ),
                    InlineKeyboardButton(
                        "🖥 Серверы",
                        callback_data=build_mode_action_callback_data(self.mode_id, "servers", session=session),
                    ),
                ]
            )
            rows.append(
                [
                    InlineKeyboardButton(
                        "🤖 Autonomy",
                        callback_data=build_mode_action_callback_data(self.mode_id, "autonomy_status", session=session),
                    ),
                ]
            )
        rows.append([InlineKeyboardButton(str(back_text or "⬅️ Назад"), callback_data=str(back_callback or "sess_active"))])
        return text, InlineKeyboardMarkup(rows)

    @staticmethod
    def _is_session_muted(session: Any) -> bool:
        raw = getattr(session, "admin_muted_until_ts", None)
        try:
            ts = float(raw) if raw is not None else 0.0
        except Exception:
            return False
        return ts > time.time()
