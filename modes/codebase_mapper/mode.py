from __future__ import annotations

import asyncio
import logging
import os
import shutil
from typing import Any, Dict, Optional

import yaml
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from session import Session

from app.mode_dependencies import ModeDependencies
from app.services.tool_availability import is_tool_available
from modes.codebase_mapper.ui import build_codebase_mapper_menu
from modes.codebase_mapper.runtime import CodebaseMapperRuntime
from modes.sdk import BaseMode, CallbackModel, MessageModel, ToolResult
from modes.sdk.services.callback_data import (
    build_mode_action_callback_data,
    build_session_mode_pick_callback_data,
)
from modes.sdk.session_busy import is_session_busy
from sessions.session_state_access import get_orchestrator_last_mode_output


class CodebaseMapperMode(BaseMode):
    mode_id = "codebase_mapper"
    display_name = "🗺 Mapper"
    description = "Строит карту кодовой базы и обновляет её по git diff"

    def __init__(self, dependencies: Optional[ModeDependencies] = None) -> None:
        super().__init__(dependencies)
        self._log = logging.getLogger(__name__)
        self._prompts: Dict[str, str] = {}
        self._active_mapper_sessions: Dict[int, Session] = {}
        self._review_page_size = 8

    def framework_sends_output(self) -> bool:
        return False

    def build_runtime(self, config: Any) -> Any:
        _ = config
        return CodebaseMapperRuntime(mode=self)

    async def on_enable(self, ctx: Dict[str, Any]) -> Optional[ToolResult]:
        session = ctx.get("session")
        bot_app = ctx.get("bot_app")
        if not session or not bot_app:
            return None
        # Mapper must use currently active CLI of the session (no routing by work type).
        await self._activate_mode(session=session, bot_app=bot_app, cli_work_type=None, executor_profile="default")
        return None

    async def on_disable(self, ctx: Dict[str, Any]) -> Optional[ToolResult]:
        session = ctx.get("session")
        bot_app = ctx.get("bot_app")
        if not session or not bot_app:
            return None
        self._interrupt_active_mapper_sessions()
        await self._deactivate_mode(session=session, bot_app=bot_app, cancel_tasks=True, timeout_s=0.2)
        return None

    async def handle_input(self, message: MessageModel, ctx: Dict[str, Any]) -> ToolResult:
        bot_app = ctx.get("bot_app")
        session = ctx.get("session")
        context = ctx.get("context")
        chat_id = self._normalize_callback_chat_id(message.chat_id)
        if not bot_app or not session:
            return ToolResult.fail("missing_context")

        msg_user_id = int(message.user_id) if getattr(message, "user_id", None) is not None else None
        dest = self._normalize_dest(ctx_dest=ctx.get("dest"), chat_id=chat_id, user_id=msg_user_id)

        ms = self._messaging(bot_app=bot_app, context=context)
        if await self._enqueue_if_busy(session=session, bot_app=bot_app, ms=ms, chat_id=chat_id, text=message.text, dest=dest):
            return ToolResult.ok()
        await ms.send_text(
            chat_id,
            (
                "🗺 Mapper активен.\n"
                "Текстовый ввод в этом режиме не обрабатывается.\n"
                "Используйте кнопки в `/sessions` → `Mapper`."
            ),
            md2=True,
        )
        return ToolResult.ok()

    async def run_pipeline(
        self,
        *,
        session: Any,
        user_text: str,
        bot_app: Any,
        context: Any,
        dest: Dict[str, Any],
    ) -> str:
        await self._silent_git_checkpoint(session, "before_start")
        try:
            runtime_getter = self._runtime_getter()
            mapper = runtime_getter("codebase_mapper_run")
            if mapper is None:
                raise RuntimeError("Codebase mapper runtime is not configured")
            defaults = getattr(getattr(bot_app, "config", None), "defaults", None)
            usage = str(getattr(defaults, "codebase_mapper_usage", "auto") or "auto")
            user_text_norm = str(user_text or "").strip().lower()
            operation = {
                "run": "run",
                "force": "force",
                "init": "init",
                "init_full": "init_full",
                "verify": "verify",
                "validate": "validate",
                "repair": "repair",
            }.get(user_text_norm, "run")
            force = operation in {"force", "init_full"}
            result = await mapper.maybe_run(
                session=session,
                workdir=session.workdir,
                usage=usage,
                force=force,
                prompt_templates=self._load_prompts(),
                operation=operation,
                sync_agents=operation in {"init", "init_full", "verify"},
                cli_runner=lambda task: self._run_mapper_focus_cli(
                    task=task,
                    base_session=session,
                    bot_app=bot_app,
                    context=context,
                    dest=dest,
                ),
            )
            status = str(result.get("status") or "").strip()
            reason = str(result.get("reason") or "").strip()
            updated = list(result.get("updated_docs") or [])
            changed = list(result.get("changed_files") or [])
            graph_state = str(result.get("graph_state") or "").strip()
            graph_nodes = int(result.get("graph_nodes") or 0)
            graph_tree = list(result.get("graph_tree") or [])
            lines = ["🗺 Codebase map", f"Status: {status}"]
            if reason:
                lines.append(f"Reason: {reason}")
            if graph_state:
                lines.append(f"Graph state: {graph_state}")
            if graph_nodes:
                lines.append(f"Graph nodes: {graph_nodes}")
            if updated:
                lines.append("Updated docs:")
                lines.extend([f"- {name}" for name in updated])
            if changed:
                lines.append(f"Changed files: {len(changed)}")
            if graph_tree:
                lines.append("Graph tree:")
                lines.extend([f"  {line}" for line in graph_tree[:40]])
            map_dir = str(result.get("map_dir") or "").strip()
            if map_dir:
                lines.append(f"Path: {map_dir}")
            return "\n".join(lines)
        finally:
            await self._silent_git_checkpoint(session, "before_finish")

    @staticmethod
    async def _run_git(workdir: str, args: list[str]) -> tuple[int, str]:
        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_PAGER"] = "cat"
        try:
            proc = await asyncio.create_subprocess_exec(
                "git",
                *args,
                cwd=workdir,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            out, _ = await proc.communicate()
            return int(proc.returncode or 0), (out or b"").decode(errors="ignore")
        except FileNotFoundError:
            return 127, "git: command not found"
        except Exception as exc:
            return 1, f"git: failed to run: {exc}"

    def _git_is_usable(self, workdir: str) -> bool:
        wd = os.path.abspath(str(workdir or "."))
        if not os.path.isdir(wd):
            return False
        if not shutil.which("git"):
            return False
        d = wd
        while True:
            if os.path.exists(os.path.join(d, ".git")):
                return True
            parent = os.path.dirname(d)
            if parent == d:
                return False
            d = parent

    async def _silent_git_checkpoint(self, session: Any, label: str) -> bool:
        workdir = str(getattr(session, "workdir", "") or "").strip()
        if not workdir or not self._git_is_usable(workdir):
            return False
        code, status_out = await self._run_git(workdir, ["status", "--porcelain"])
        if code != 0 or not (status_out or "").strip():
            return False
        code, add_out = await self._run_git(workdir, ["add", "-A"])
        if code != 0:
            self._log.warning("codebase_mapper checkpoint add failed (%s): %s", label, add_out[:200])
            return False
        msg = f"[CodebaseMapper] checkpoint: {str(label or 'checkpoint').strip()}"
        if len(msg) > 100:
            msg = msg[:100].rstrip()
        code, commit_out = await self._run_git(workdir, ["commit", "-m", msg])
        if code != 0:
            self._log.warning("codebase_mapper checkpoint commit failed (%s): %s", label, commit_out[:200])
            return False
        return True

    def _mode_root(self) -> str:
        return os.path.dirname(__file__)

    def _load_prompts(self) -> Dict[str, str]:
        if self._prompts:
            return self._prompts
        path = os.path.join(self._mode_root(), "prompts.yaml")
        raw: Dict[str, Any] = {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
        except Exception:
            self._log.exception("codebase_mapper prompts read failed: %s", path)
            raw = {}
        prompts = raw.get("prompts") if isinstance(raw, dict) else {}
        if not isinstance(prompts, dict):
            prompts = {}
        self._prompts = {str(k): str(v) for k, v in prompts.items()}
        return self._prompts

    async def _run_mapper_focus_cli(
        self,
        *,
        task: Dict[str, Any],
        base_session: Any,
        bot_app: Any,
        context: Any,
        dest: Dict[str, Any],
    ) -> Dict[str, Any]:
        focus = str(task.get("focus") or "").strip() or "mapper"
        prompt = str(task.get("prompt") or "").strip()
        if not prompt:
            return {"success": False, "error": "empty_prompt", "focus": focus}

        tooling = self._tooling()
        session = self._clone_cli_session(base_session=base_session, focus=focus)
        self._active_mapper_sessions[id(session)] = session
        try:
            tool_ctx = self._build_tool_ctx(session=session, context=context, dest=dest, bot_app=bot_app, focus=focus)
            response = await tooling.execute(
                "use_cli",
                {"task_text": prompt, "fresh_run": True},
                tool_ctx,
            )
            if not response.get("success"):
                err = str(response.get("error") or "use_cli_failed").strip()
                raise RuntimeError(f"mapper focus={focus} failed: {err}")
            return {
                "success": True,
                "focus": focus,
                "output": str(response.get("output") or ""),
                "target_docs": list(task.get("target_docs") or []),
            }
        finally:
            self._active_mapper_sessions.pop(id(session), None)

    def _clone_cli_session(self, *, base_session: Any, focus: str) -> Session:
        sid = str(getattr(base_session, "id", "mapper") or "mapper")
        base_active_cli = getattr(base_session, "active_cli", None)
        active_cli = base_active_cli
        config = getattr(base_session, "config", None)
        qwen_available = False
        try:
            if config is not None and is_tool_available(config, "qwen"):
                qwen_available = True
                active_cli = "qwen"
        except Exception:
            self._log.exception("codebase_mapper qwen availability check failed")
        self._log.info(
            "codebase_mapper cli select focus=%s base_active_cli=%s qwen_available=%s selected_cli=%s",
            focus,
            base_active_cli,
            qwen_available,
            active_cli,
        )
        # Важно: передаём active_cli в конструктор, чтобы __post_init__() не сбросил его к tool.name
        clone = Session(
            id=f"{sid}:codebase_mapper:{focus}",
            tool=getattr(base_session, "tool"),
            workdir=str(getattr(base_session, "workdir", "") or ""),
            idle_timeout_sec=int(getattr(base_session, "idle_timeout_sec", 120) or 120),
            config=getattr(base_session, "config"),
            name=getattr(base_session, "name", None),
            active_cli=active_cli,
        )
        # resume_tokens копируем после создания, чтобы __post_init__() корректно инициализировал структуру
        clone.resume_tokens = dict(getattr(base_session, "resume_tokens", {}) or {})
        clone.cli_work_type = getattr(base_session, "cli_work_type", None)
        clone.executor_profile = getattr(base_session, "executor_profile", None)
        return clone

    def _build_tool_ctx(
        self,
        *,
        session: Session,
        context: Any,
        dest: Dict[str, Any],
        bot_app: Any,
        focus: str,
    ) -> Dict[str, Any]:
        chat_id = dest.get("chat_id") if isinstance(dest, dict) else None
        chat_type = dest.get("chat_type") if isinstance(dest, dict) else None
        corr_session = str(getattr(session, "id", "unknown") or "unknown")
        return {
            "cwd": getattr(session, "workdir", None),
            "state_root": getattr(session, "workdir", None),
            "session_id": getattr(session, "id", None),
            "chat_id": chat_id,
            "chat_type": chat_type,
            "bot": bot_app,
            "context": context,
            "session": session,
            "allowed_tools": ["All"],
            "tool_timeouts_ms": {"use_cli": 3600 * 1000},
            "corr_id": f"codebase_mapper:{corr_session}:{focus}",
        }

    async def handle_callback(self, callback: CallbackModel, ctx: Dict[str, Any]) -> ToolResult:
        bot_app = ctx.get("bot_app")
        session = ctx.get("session")
        context = ctx.get("context")
        query = ctx.get("query")
        chat_id = self._normalize_callback_chat_id(callback.chat_id)
        if not bot_app or not session:
            return ToolResult.fail("missing_context")

        ms = self._messaging(bot_app=bot_app, context=context)
        action = str(callback.action or "").strip()

        if action in ("enable", "on"):
            await self._activate_mode(session=session, bot_app=bot_app, cli_work_type=None, executor_profile="default")
            await self._rerender_menu(bot_app, session, chat_id, context, query, note="Маппер включен.")
            return ToolResult.ok()

        if action in ("disable", "off"):
            await self._deactivate_mode(session=session, bot_app=bot_app, cancel_tasks=True, timeout_s=0.2)
            await self._rerender_menu(bot_app, session, chat_id, context, query, note="Маппер выключен.")
            return ToolResult.ok()

        if action in ("run", "refresh"):
            force = action == "refresh"
            if self._is_mapper_operation_busy(bot_app=bot_app, session=session):
                await ms.send_or_edit(
                    query=query,
                    chat_id=chat_id,
                    text="🗺 Обновление карты уже выполняется.",
                    md2=True,
                    reply_markup=None,
                )
                return ToolResult.ok()

            await self._start_background_pipeline(
                bot_app=bot_app,
                session=session,
                context=context,
                chat_id=chat_id,
                prompt="force" if force else "run",
            )
            await ms.send_or_edit(
                query=query,
                chat_id=chat_id,
                text="🗺 Обновление карты запущено.\nСтатус сессии: занята.",
                md2=True,
                reply_markup=None,
            )
            return ToolResult.ok()

        if action in {"validate", "repair"}:
            if self._is_mapper_operation_busy(bot_app=bot_app, session=session):
                await ms.send_or_edit(
                    query=query,
                    chat_id=chat_id,
                    text="🗺 Операция маппера уже выполняется.",
                    md2=True,
                    reply_markup=None,
                )
                return ToolResult.ok()
            await self._start_background_pipeline(
                bot_app=bot_app,
                session=session,
                context=context,
                chat_id=chat_id,
                prompt="repair",
            )
            await ms.send_or_edit(
                query=query,
                chat_id=chat_id,
                text="🛠 Validate + Repair запущен.",
                md2=True,
                reply_markup=None,
            )
            return ToolResult.ok()

        if action == "init":
            runtime_getter = self._runtime_getter()
            mapper = runtime_getter("codebase_mapper_status")
            data = mapper.get_status(workdir=session.workdir) if mapper is not None else {}
            initialized = bool(data.get("graph_initialized"))
            if initialized:
                keyboard = InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "♻️ Переинициализировать полностью",
                                callback_data=build_mode_action_callback_data(
                                    self.mode_id,
                                    "init_choice",
                                    session=session,
                                    payload="full",
                                ),
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                "🩺 Детальная проверка и исправления",
                                callback_data=build_mode_action_callback_data(
                                    self.mode_id,
                                    "init_choice",
                                    session=session,
                                    payload="verify",
                                ),
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                "❌ Отмена",
                                callback_data=build_mode_action_callback_data(
                                    self.mode_id,
                                    "init_choice",
                                    session=session,
                                    payload="cancel",
                                ),
                            )
                        ],
                    ]
                )
                await ms.send_or_edit(
                    query=query,
                    chat_id=chat_id,
                    text=(
                        "Граф уже инициализирован.\n\n"
                        "Выберите действие:\n"
                        "1. Полная переинициализация\n"
                        "2. Детальная проверка + repair\n"
                        "3. Отмена"
                    ),
                    md2=True,
                    reply_markup=keyboard,
                )
                return ToolResult.ok()
            if self._is_mapper_operation_busy(bot_app=bot_app, session=session):
                await ms.send_or_edit(
                    query=query,
                    chat_id=chat_id,
                    text="🗺 Операция маппера уже выполняется.",
                    md2=True,
                    reply_markup=None,
                )
                return ToolResult.ok()
            await self._start_background_pipeline(
                bot_app=bot_app,
                session=session,
                context=context,
                chat_id=chat_id,
                prompt="init",
            )
            await ms.send_or_edit(
                query=query,
                chat_id=chat_id,
                text="🧭 Инициализация md-графа запущена.",
                md2=True,
                reply_markup=None,
            )
            return ToolResult.ok()

        if action == "init_choice":
            choice = str((callback.payload or {}).get("value") or "").strip().lower()
            if choice == "cancel":
                await self._rerender_menu(bot_app, session, chat_id, context, query, note="Инициализация отменена.")
                return ToolResult.ok()
            if choice == "full":
                prompt = "init_full"
                notice = "♻️ Полная переинициализация запущена."
            elif choice == "verify":
                prompt = "verify"
                notice = "🩺 Детальная проверка графа запущена."
            else:
                return ToolResult.fail("unknown_init_choice")
            if self._is_mapper_operation_busy(bot_app=bot_app, session=session):
                await ms.send_or_edit(
                    query=query,
                    chat_id=chat_id,
                    text="🗺 Операция маппера уже выполняется.",
                    md2=True,
                    reply_markup=None,
                )
                return ToolResult.ok()
            await self._start_background_pipeline(
                bot_app=bot_app,
                session=session,
                context=context,
                chat_id=chat_id,
                prompt=prompt,
            )
            await ms.send_or_edit(query=query, chat_id=chat_id, text=notice, md2=True, reply_markup=None)
            return ToolResult.ok()

        if action == "status":
            runtime_getter = self._runtime_getter()
            mapper = runtime_getter("codebase_mapper_status")
            if mapper is None:
                await ms.send_or_edit(query=query, chat_id=chat_id, text="Mapper runtime недоступен.", md2=True)
                return ToolResult.ok()
            data = mapper.get_status(workdir=session.workdir)
            status = str(data.get("status") or "").strip()
            docs = list(data.get("docs") or [])
            generated_at = str(data.get("generated_at") or "").strip()
            graph_state = str(data.get("graph_state") or "").strip()
            graph_nodes = int(data.get("graph_nodes") or 0)
            graph_tree = list(data.get("graph_tree") or [])
            rules_needs_review = int(data.get("rules_needs_review") or 0)
            needs_review_items = list(data.get("needs_review_items") or [])
            inferred_total = int(data.get("inferred_rules_total") or 0)
            inferred_active = int(data.get("inferred_rules_active") or 0)
            inferred_proposed = int(data.get("inferred_rules_proposed") or 0)
            validate_queue = list(data.get("validate_queue") or [])
            repair_queue = list(data.get("repair_queue") or [])
            degraded_nodes = list(data.get("degraded_nodes") or [])
            nodes_status_counts = dict(data.get("nodes_status_counts") or {})
            lines = ["🗺 Статус карты кодовой базы", f"Status: {status}", f"Docs: {len(docs)}"]
            if generated_at:
                lines.append(f"Generated: {generated_at}")
            lines.append(f"Graph: {graph_state or 'empty'}")
            lines.append(f"Graph nodes: {graph_nodes}")
            lines.append(f"Inferred rules: {inferred_total} (active: {inferred_active}, proposed: {inferred_proposed})")
            lines.append(f"Needs review: {rules_needs_review}")
            if nodes_status_counts:
                lines.append(
                    "Node states: "
                    f"ok={int(nodes_status_counts.get('ok') or 0)}, "
                    f"needs_repair={int(nodes_status_counts.get('needs_repair') or 0)}, "
                    f"degraded={int(nodes_status_counts.get('degraded') or 0)}, "
                    f"invalid={int(nodes_status_counts.get('invalid') or 0)}"
                )
            lines.append(f"Validate queue: {len(validate_queue)}")
            lines.append(f"Repair queue: {len(repair_queue)}")
            if degraded_nodes:
                lines.append(f"Degraded nodes: {len(degraded_nodes)}")
            if docs:
                lines.extend([f"- {name}" for name in docs])
            if needs_review_items:
                lines.append("Review queue:")
                lines.extend([f"- {name}" for name in needs_review_items[:10]])
            if repair_queue:
                lines.append("Repair items:")
                lines.extend([f"- {name}" for name in repair_queue[:10]])
            if degraded_nodes:
                lines.append("Degraded:")
                lines.extend([f"- {name}" for name in degraded_nodes[:10]])
            if graph_tree:
                lines.append("Graph tree:")
                lines.extend([f"  {line}" for line in graph_tree[:40]])
            await ms.send_or_edit(query=query, chat_id=chat_id, text="\n".join(lines), md2=True)
            return ToolResult.ok()

        if action == "review":
            await self._render_review_page(
                bot_app=bot_app,
                session=session,
                chat_id=chat_id,
                context=context,
                query=query,
                page=0,
            )
            return ToolResult.ok()

        if action == "review_page":
            page = self._parse_int((callback.payload or {}).get("value"), default=0)
            await self._render_review_page(
                bot_app=bot_app,
                session=session,
                chat_id=chat_id,
                context=context,
                query=query,
                page=page,
            )
            return ToolResult.ok()

        if action == "review_confirm":
            idx = self._parse_int((callback.payload or {}).get("value"), default=-1)
            runtime_getter = self._runtime_getter()
            mapper = runtime_getter("codebase_mapper_status")
            if mapper is None:
                await ms.send_or_edit(query=query, chat_id=chat_id, text="Mapper runtime недоступен.", md2=True)
                return ToolResult.ok()
            review_data = mapper.list_review_items(workdir=session.workdir)
            items = list(review_data.get("items") or [])
            if idx < 0 or idx >= len(items):
                await ms.send_or_edit(query=query, chat_id=chat_id, text="Элемент ревью не найден.", md2=True)
                return ToolResult.ok()
            item = str(items[idx] or "")
            result = mapper.confirm_review_item(workdir=session.workdir, item=item)
            if not bool(result.get("ok")):
                await ms.send_or_edit(query=query, chat_id=chat_id, text="Не удалось подтвердить элемент ревью.", md2=True)
                return ToolResult.ok()
            page = idx // self._review_page_size
            await self._render_review_page(
                bot_app=bot_app,
                session=session,
                chat_id=chat_id,
                context=context,
                query=query,
                page=page,
                note=f"Подтверждено: `{item}`",
            )
            return ToolResult.ok()

        return ToolResult.fail("unknown_action")

    def _is_mapper_operation_busy(self, *, bot_app: Any, session: Any) -> bool:
        run_lock = getattr(session, "run_lock", None)
        if is_session_busy(session, run_lock):
            return True
        return bool(self._mode_task_names(bot_app=bot_app, session=session))

    def _interrupt_active_mapper_sessions(self) -> None:
        sessions = list(self._active_mapper_sessions.values())
        self._active_mapper_sessions.clear()
        for mapper_session in sessions:
            try:
                mapper_session.interrupt()
            except Exception:
                self._log.exception("codebase_mapper interrupt clone failed")

    async def _start_background_pipeline(
        self,
        *,
        bot_app: Any,
        session: Any,
        context: Any,
        chat_id: int,
        prompt: str,
    ) -> None:
        async def _run() -> None:
            pipeline = self._pipeline()
            ms = self._messaging(bot_app=bot_app, context=context)
            deactivated = False
            try:
                await pipeline.run_mode_pipeline(
                    session,
                    prompt,
                    {"kind": "telegram", "chat_id": chat_id},
                    context,
                    mode_id=self.mode_id,
                )
                output = str(get_orchestrator_last_mode_output(session, "") or "").strip()
                if not output:
                    output = "🗺 Операция Codebase Mapper завершена."
                else:
                    output = f"✅ Операция Codebase Mapper завершена.\n\n{output}"
                await self._deactivate_mode(session=session, bot_app=bot_app, cancel_tasks=False)
                deactivated = True
                await ms.send_text(chat_id, output, md2=True)
            except asyncio.CancelledError:
                self._interrupt_active_mapper_sessions()
                raise
            finally:
                if not deactivated:
                    await self._deactivate_mode(session=session, bot_app=bot_app, cancel_tasks=False)

        await self._activate_mode(session=session, bot_app=bot_app, cli_work_type=None, executor_profile="default")
        self._start_mode_task(bot_app=bot_app, session=session, coro=_run(), name="run_mapper")

    async def _render_review_page(
        self,
        *,
        bot_app: Any,
        session: Any,
        chat_id: int,
        context: Any,
        query: Any,
        page: int,
        note: str = "",
    ) -> None:
        runtime_getter = self._runtime_getter()
        mapper = runtime_getter("codebase_mapper_status")
        ms = self._messaging(bot_app=bot_app, context=context)
        if mapper is None:
            await ms.send_or_edit(query=query, chat_id=chat_id, text="Mapper runtime недоступен.", md2=True)
            return
        review_data = mapper.list_review_items(workdir=session.workdir)
        items = list(review_data.get("items") or [])
        needs_review = set(str(x) for x in list(review_data.get("needs_review") or []))
        reviewed = dict(review_data.get("reviewed") or {})
        total = len(items)
        page_size = max(1, int(self._review_page_size))
        max_page = max(0, (total - 1) // page_size) if total else 0
        cur_page = min(max(0, int(page)), max_page)
        start = cur_page * page_size
        end = min(total, start + page_size)
        page_items = items[start:end]

        rows = []
        for idx, item in enumerate(page_items, start=start):
            is_reviewed = bool(reviewed.get(item))
            prefix = "✅" if is_reviewed else ("⚠️" if item in needs_review else "•")
            label = f"{prefix} {item}"
            rows.append(
                [
                    InlineKeyboardButton(
                        self._shorten_label(label, 56),
                        callback_data=build_mode_action_callback_data(
                            self.mode_id,
                            "review_confirm",
                            session=session,
                            payload=str(idx),
                        ),
                    )
                ]
            )

        nav = []
        if cur_page > 0:
            nav.append(
                InlineKeyboardButton(
                    "◀️",
                    callback_data=build_mode_action_callback_data(
                        self.mode_id,
                        "review_page",
                        session=session,
                        payload=str(cur_page - 1),
                    ),
                )
            )
        if cur_page < max_page:
            nav.append(
                InlineKeyboardButton(
                    "▶️",
                    callback_data=build_mode_action_callback_data(
                        self.mode_id,
                        "review_page",
                        session=session,
                        payload=str(cur_page + 1),
                    ),
                )
            )
        if nav:
            rows.append(nav)
        rows.append([InlineKeyboardButton("⬅️ Назад", callback_data=build_session_mode_pick_callback_data(session, self.mode_id))])

        text_lines = [
            "🧾 Ревью графа",
            f"Элементов: {total}",
            f"Needs review: {len(needs_review)}",
            f"Страница: {cur_page + 1}/{max_page + 1 if total else 1}",
            "",
            "Нажмите на элемент, чтобы подтвердить.",
        ]
        if note:
            text_lines.insert(0, note)
        await ms.send_or_edit(
            query=query,
            chat_id=chat_id,
            text="\n".join(text_lines),
            md2=True,
            reply_markup=InlineKeyboardMarkup(rows),
        )

    @staticmethod
    def _parse_int(raw: Any, default: int = 0) -> int:
        try:
            return int(str(raw).strip())
        except Exception:
            return int(default)

    @staticmethod
    def _shorten_label(text: str, limit: int) -> str:
        s = str(text or "").strip()
        n = max(8, int(limit or 56))
        if len(s) <= n:
            return s
        return s[: n - 1] + "…"

    async def _rerender_menu(self, bot_app: Any, session: Any, chat_id: int, context: Any, query: Any, *, note: str = "") -> None:
        await self._rerender_menu_common(
            bot_app=bot_app,
            session=session,
            chat_id=chat_id,
            context=context,
            query=query,
            note=note,
            back_callback="sess_active",
            back_text="⬅️ Назад",
        )

    def build_menu(
        self,
        session: Any,
        back_callback: str = "sess_active",
        back_text: str = "⬅️ Назад",
    ) -> tuple[str, Any]:
        init_label = "🧭 Инициализировать граф"
        try:
            runtime_getter = self._runtime_getter()
            mapper = runtime_getter("codebase_mapper_status")
            if mapper is not None:
                status = mapper.get_status(workdir=getattr(session, "workdir", ""))
                if bool(status.get("graph_initialized")):
                    init_label = "🧭 Обновить граф"
        except Exception:
            self._log.exception("codebase_mapper build_menu: status lookup failed")
        return build_codebase_mapper_menu(
            session,
            back_callback=back_callback,
            back_text=back_text,
            mode_id=self.mode_id,
            init_label=init_label,
        )
