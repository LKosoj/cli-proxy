from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import yaml
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from tg.markdown import escape_markdown_v2_all

from app.mode_dependencies import ModeDependencies
from modes.sdk import BaseMode, CallbackModel, MessageModel, MessagingService, ToolResult
from modes.sdk.runtime.openai_client import chat_completion
from modes.sdk.session_busy import is_session_busy
from modes.sdk.services.callback_data import build_mode_action_callback_data
from sessions.session_state_access import (
    get_active_mode,
    get_orchestrator_last_mode_output,
    is_orchestrator_enabled,
    set_active_mode,
    set_orchestrator_enabled,
)

from .artifacts import render_tasks_md
from .constitution import load_constitution
from .ears import extract_clarification_questions
from .handoff import run_handoff_to_manager
from .phases import (
    ModelCall,
    allocate_spec_dir,
    generate_plan,
    generate_spec,
    generate_tasks,
    next_phase,
    normalize_spec_dir,
    slugify,
)
from .project_init import classify_project, run_project_initialization
from .state import clear_sdd_gate, get_sdd_state, set_sdd_phase

_log = logging.getLogger(__name__)


class SddMode(BaseMode):
    mode_id = "sdd"
    display_name = "📐 SDD"
    description = "Spec-Driven Development: specify → plan → tasks через гейты подтверждения"

    def __init__(self, dependencies: Optional[ModeDependencies] = None) -> None:
        super().__init__(dependencies)
        self._log = logging.getLogger(__name__)

    def framework_sends_output(self) -> bool:
        return False

    @staticmethod
    def _enable_requirements_error(bot_app: Any, session: Any) -> str:
        defaults = getattr(getattr(bot_app, "config", None), "defaults", None)
        if not getattr(defaults, "openai_api_key", None) or not getattr(defaults, "openai_model", None):
            return "Для SDD нужен OpenAI API. Настройте openai_api_key и openai_model в config.yaml."
        workdir = str(getattr(session, "workdir", "") or "")
        if not workdir or not os.path.isdir(workdir):
            return "Для SDD нужна рабочая директория. Создайте сессию через /sessions."
        return ""

    # ------------------------------------------------------------------
    # LLM seam
    # ------------------------------------------------------------------

    async def _chat_completion(
        self,
        bot_app: Any,
        system: str,
        user: str,
        *,
        response_format: Optional[Dict[str, Any]] = None,
    ) -> str:
        runtime_getter = self._optional_runtime_getter()
        runtime = runtime_getter("sdd_chat_completion") if callable(runtime_getter) else None
        if runtime is not None and hasattr(runtime, "chat_completion"):
            return str(
                await runtime.chat_completion(
                    bot_app.config,
                    str(system or ""),
                    str(user or ""),
                    response_format=response_format,
                )
                or ""
            )
        return str(
            await chat_completion(
                bot_app.config,
                str(system or ""),
                str(user or ""),
                response_format=response_format,
            )
            or ""
        )

    def _model_call(self, bot_app: Any) -> ModelCall:
        """Return a ModelCall closure that hides bot_app from phases layer."""
        async def _call(system: str, user: str) -> str:
            return await self._chat_completion(
                bot_app, system, user, response_format={"type": "json_object"}
            )
        return _call

    # ------------------------------------------------------------------
    # Prompts
    # ------------------------------------------------------------------

    def _load_prompts(self) -> dict:
        path = Path(__file__).with_name("prompts.yaml")
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        return dict((data or {}).get("prompts") or {})

    # ------------------------------------------------------------------
    # Gate keyboard
    # ------------------------------------------------------------------

    def _gate_keyboard(self, session: Any, prompts: dict) -> InlineKeyboardMarkup:
        btn_accept = str(prompts.get("btn_accept") or "✅ Принять")
        btn_revise = str(prompts.get("btn_revise") or "✍️ Правки")
        btn_stop = str(prompts.get("btn_stop") or "⏹ Остановить")
        rows = [
            [
                InlineKeyboardButton(
                    btn_accept,
                    callback_data=build_mode_action_callback_data("sdd", "gate_accept", session=session),
                ),
                InlineKeyboardButton(
                    btn_revise,
                    callback_data=build_mode_action_callback_data("sdd", "gate_revise", session=session),
                ),
            ],
            [
                InlineKeyboardButton(
                    btn_stop,
                    callback_data=build_mode_action_callback_data("sdd", "gate_stop", session=session),
                ),
            ],
        ]
        return InlineKeyboardMarkup(rows)

    # ------------------------------------------------------------------
    # Phase runner
    # ------------------------------------------------------------------

    async def _run_phase(
        self,
        *,
        session: Any,
        bot_app: Any,
        context: Any,
        dest: Dict[str, Any],
        phase: str,
        revision: str = "",
        restore_gate_on_failure: str = "",
    ) -> None:
        ms = self._messaging(bot_app=bot_app, context=context)
        chat_id = int(dest.get("chat_id") or 0)
        sdd = get_sdd_state(session)

        def _restore_failure_gate() -> None:
            restore_gate = str(restore_gate_on_failure or "").strip()
            if restore_gate:
                sdd.pending_gate = restore_gate
                sdd.last_action = "gate_revise"
                self._persist_sessions(bot_app)

        workdir = str(getattr(session, "workdir", "") or "")
        if not workdir:
            _restore_failure_gate()
            await ms.send_text(chat_id, "❌ Рабочая директория не задана. Создайте сессию через /sessions.", md2=False)
            return
        raw_spec_dir = str(sdd.spec_dir or "")
        spec_dir = ""
        if raw_spec_dir:
            normalized_spec_dir = normalize_spec_dir(workdir, raw_spec_dir)
            if not normalized_spec_dir:
                _restore_failure_gate()
                await ms.send_text(
                    chat_id,
                    "❌ Каталог спецификации небезопасен или находится вне `specs/`\\.",
                    md2=True,
                )
                return
            spec_dir = normalized_spec_dir
            if spec_dir != raw_spec_dir:
                sdd.spec_dir = spec_dir
                self._persist_sessions(bot_app)
        elif phase != "specify":
            _restore_failure_gate()
            await ms.send_text(
                chat_id,
                "❌ Каталог спецификации не задан — сначала запустите фазу `specify`\\.",
                md2=True,
            )
            return
        constitution = load_constitution(workdir)
        prompts = self._load_prompts()
        model = self._model_call(bot_app)

        try:
            if phase == "specify":
                intent = str(sdd.source_intent or "")
                await ms.send_text(chat_id, "⏳ Генерирую спецификацию...", md2=False)
                spec_md, payload = await generate_spec(
                    model, intent=intent, constitution=constitution, prompts=prompts, revision=revision
                )
                if not spec_dir:
                    # Свежий каталог: slug из LLM-ответа, но обязательно через slugify
                    # (защита от path traversal — LLM может вернуть "../" или вложенный путь).
                    slug = slugify(str(payload.get("feature_slug") or sdd.feature_slug or intent))
                    sdd.feature_slug = slug
                    spec_dir = allocate_spec_dir(workdir, slug)
                    sdd.spec_dir = spec_dir
                else:
                    # spec_dir и feature_slug уже согласованы в _init_and_run_specify —
                    # не перетираем feature_slug расходящимся LLM-слагом.
                    slug = str(sdd.feature_slug or "")
                os.makedirs(spec_dir, exist_ok=True)
                spec_path = os.path.join(spec_dir, "spec.md")
                with open(spec_path, "w", encoding="utf-8") as fh:
                    fh.write(spec_md)
                questions = extract_clarification_questions(payload)
                if questions:
                    clarf_path = os.path.join(spec_dir, "clarifications.md")
                    clarf_lines = ["# Clarifications", "", "## Questions", ""]
                    clarf_lines += [f"{i}. {q}" for i, q in enumerate(questions, 1)]
                    clarf_lines += [
                        "", "## Answers", "",
                        "_(ответьте боту через кнопку «Правки» — файл перегенерируется при доработке)_",
                        "",
                    ]
                    with open(clarf_path, "w", encoding="utf-8") as fh:
                        fh.write("\n".join(clarf_lines))
                    q_text = "\n".join(f"{i}. {q}" for i, q in enumerate(questions, 1))
                    await ms.send_text(
                        chat_id,
                        f"⚠️ Спецификация содержит вопросы для уточнения:\n\n{q_text}\n\nОтветьте через кнопку «Правки».",
                        md2=False,
                    )
                set_sdd_phase(session, "specify")
                sdd.pending_gate = "specify"
                self._persist_sessions(bot_app)
                gate_text = str(prompts.get("gate_spec_header") or "📋 *Спецификация готова*")
                gate_text = gate_text.replace("{slug}", slug)
                await ms.send_text(chat_id, gate_text, md2=True, reply_markup=self._gate_keyboard(session, prompts))

            elif phase == "plan":
                spec_path = os.path.join(spec_dir, "spec.md")
                spec_md = ""
                if os.path.isfile(spec_path):
                    with open(spec_path, encoding="utf-8") as fh:
                        spec_md = fh.read()
                await ms.send_text(chat_id, "⏳ Генерирую архитектурный план...", md2=False)
                plan_md, _ = await generate_plan(
                    model, spec_md=spec_md, constitution=constitution, prompts=prompts, revision=revision
                )
                os.makedirs(spec_dir, exist_ok=True)
                plan_path = os.path.join(spec_dir, "plan.md")
                with open(plan_path, "w", encoding="utf-8") as fh:
                    fh.write(plan_md)
                set_sdd_phase(session, "plan")
                sdd.pending_gate = "plan"
                self._persist_sessions(bot_app)
                gate_text = str(prompts.get("gate_plan_header") or "🏗 *Архитектурный план готов*")
                await ms.send_text(chat_id, gate_text, md2=True, reply_markup=self._gate_keyboard(session, prompts))

            elif phase == "tasks":
                spec_path = os.path.join(spec_dir, "spec.md")
                plan_path = os.path.join(spec_dir, "plan.md")
                spec_md = ""
                plan_md = ""
                if os.path.isfile(spec_path):
                    with open(spec_path, encoding="utf-8") as fh:
                        spec_md = fh.read()
                if os.path.isfile(plan_path):
                    with open(plan_path, encoding="utf-8") as fh:
                        plan_md = fh.read()
                await ms.send_text(chat_id, "⏳ Генерирую список задач...", md2=False)
                project_plan = await generate_tasks(
                    model, spec_md=spec_md, plan_md=plan_md, constitution=constitution, prompts=prompts,
                    revision=revision,
                )
                os.makedirs(spec_dir, exist_ok=True)
                tasks_path = os.path.join(spec_dir, "tasks.md")
                with open(tasks_path, "w", encoding="utf-8") as fh:
                    fh.write(render_tasks_md(project_plan))
                set_sdd_phase(session, "tasks")
                sdd.pending_gate = "tasks"
                self._persist_sessions(bot_app)
                gate_text = str(prompts.get("gate_tasks_header") or "✅ *Декомпозиция задач готова*")
                await ms.send_text(chat_id, gate_text, md2=True, reply_markup=self._gate_keyboard(session, prompts))

        except Exception:
            self._log.exception("sdd _run_phase failed phase=%s", phase)
            _restore_failure_gate()
            try:
                await ms.send_text(chat_id, f"❌ Ошибка при генерации фазы `{phase}`\\. Проверьте логи\\.", md2=True)
            except Exception:
                self._log.exception("sdd error notify failed")

    # ------------------------------------------------------------------
    # handle_input
    # ------------------------------------------------------------------

    async def handle_input(self, message: MessageModel, ctx: Dict[str, Any]) -> ToolResult:
        bot_app = ctx.get("bot_app")
        session = ctx.get("session")
        context = ctx.get("context")
        chat_id = self._normalize_callback_chat_id(message.chat_id)
        if not bot_app or not session:
            return ToolResult.fail("missing_context")

        dest = self._normalize_dest(ctx_dest=ctx.get("dest"), chat_id=chat_id)
        ms = self._messaging(bot_app=bot_app, context=context)
        sdd = get_sdd_state(session)

        # Guard: Аналитик ещё работает — не принимаем новые намерения
        if sdd.last_action == "fork_analyst_running":
            await ms.send_text(chat_id, "⏳ Аналитик работает, дождитесь результата\\.", md2=True)
            return ToolResult.ok()

        # If there is a pending gate — check last_action for revise flow
        if sdd.pending_gate and sdd.last_action == "gate_revise":
            # Treat text as revision instructions → re-run current phase
            revision_text = str(message.text or "").strip()
            sdd.last_action = ""
            phase = str(sdd.pending_gate or sdd.phase or "specify")
            self._persist_sessions(bot_app)
            self._start_mode_task(
                bot_app=bot_app,
                session=session,
                coro=self._run_phase(
                    session=session, bot_app=bot_app, context=context, dest=dict(dest),
                    phase=phase, revision=revision_text,
                ),
                name=f"sdd_phase_{phase}",
            )
            return ToolResult.ok()

        if sdd.pending_gate:
            await ms.send_text(
                chat_id,
                "Используйте кнопки ниже для управления текущей фазой SDD\\.",
                md2=True,
            )
            return ToolResult.ok()

        # New feature intent
        intent = str(message.text or "").strip()
        if not intent:
            await ms.send_text(chat_id, "Введите описание фичи для запуска SDD\\.", md2=True)
            return ToolResult.ok()

        # Сохраняем намерение, сбрасываем фазу; spec_dir не аллоцируем — отложено до выбора пути
        sdd.source_intent = intent
        sdd.phase = "idle"
        sdd.pending_gate = None
        sdd.last_action = ""
        self._persist_sessions(bot_app)

        await self._show_fork_menu(session, bot_app, context, chat_id, dest, ms)
        return ToolResult.ok()

    # ------------------------------------------------------------------
    # handle_callback
    # ------------------------------------------------------------------

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
        dest: Dict[str, Any] = {"kind": "telegram", "chat_id": chat_id}

        handlers = {
            "menu": lambda: self._cb_menu(bot_app, session, chat_id, context, query),
            "enable": lambda: self._cb_enable(bot_app, session, ms, chat_id, context, query),
            "on": lambda: self._cb_enable(bot_app, session, ms, chat_id, context, query),
            "disable": lambda: self._cb_disable(bot_app, session, ms, chat_id, context, query),
            "off": lambda: self._cb_disable(bot_app, session, ms, chat_id, context, query),
            "status": lambda: self._cb_status(bot_app, session, ms, chat_id, context, query),
            "reset": lambda: self._cb_reset(bot_app, session, ms, chat_id, context, query),
            "init_project": lambda: self._cb_init_project(bot_app, session, ms, chat_id, context, query),
            "init_project_confirm": lambda: self._cb_init_project_confirm(
                bot_app, session, ms, chat_id, context, query
            ),
            "init_project_cancel": lambda: self._cb_init_project_cancel(
                bot_app, session, ms, chat_id, context, query
            ),
            "gate_accept": lambda: self._cb_gate_accept(bot_app, session, ms, chat_id, context, query, dest),
            "gate_revise": lambda: self._cb_gate_revise(bot_app, session, ms, chat_id, context, query),
            "gate_stop": lambda: self._cb_gate_stop(bot_app, session, ms, chat_id, context, query),
            "fork_direct": lambda: self._cb_fork_direct(bot_app, session, ms, chat_id, context, query, dest),
            "fork_analyst": lambda: self._cb_fork_analyst(bot_app, session, ms, chat_id, context, query, dest),
        }
        dispatched = await self._dispatch_callback_action(action=action, handlers=handlers)
        if dispatched is not None:
            return dispatched
        return ToolResult.fail("unknown_action")

    # ------------------------------------------------------------------
    # Callback handlers
    # ------------------------------------------------------------------

    async def _cb_menu(self, bot_app: Any, session: Any, chat_id: int, context: Any, query: Any) -> ToolResult:
        await self._rerender_menu_common(
            bot_app=bot_app, session=session, chat_id=chat_id, context=context, query=query
        )
        return ToolResult.ok()

    async def _cb_enable(
        self, bot_app: Any, session: Any, ms: MessagingService, chat_id: int, context: Any, query: Any
    ) -> ToolResult:
        error_text = self._enable_requirements_error(bot_app, session)
        if error_text:
            await ms.send_or_edit(query=query, chat_id=chat_id, text=error_text, md2=True)
            return ToolResult.fail("requirements_not_met")
        await self._activate_mode(session=session, bot_app=bot_app)
        await self._rerender_menu_common(
            bot_app=bot_app, session=session, chat_id=chat_id, context=context, query=query
        )
        return ToolResult.ok()

    async def _cb_disable(
        self, bot_app: Any, session: Any, ms: MessagingService, chat_id: int, context: Any, query: Any
    ) -> ToolResult:
        if self._is_mode_active(session):
            await self._deactivate_mode(session=session, bot_app=bot_app, cancel_tasks=True, timeout_s=0.2)
        await self._rerender_menu_common(
            bot_app=bot_app, session=session, chat_id=chat_id, context=context, query=query
        )
        return ToolResult.ok()

    async def _cb_status(
        self, bot_app: Any, session: Any, ms: MessagingService, chat_id: int, context: Any, query: Any
    ) -> ToolResult:
        sdd = get_sdd_state(session)
        enabled = str(get_active_mode(session, "") or "").strip() == self.mode_id
        text = (
            f"📐 *Статус SDD*\n\n"
            f"Режим: {'включен' if enabled else 'выключен'}\n"
            f"Фаза: `{sdd.phase}`\n"
            f"Фича: `{sdd.feature_slug or 'нет'}`\n"
            f"Гейт: `{sdd.pending_gate or 'нет'}`\n"
            f"Инициализация проекта: `{sdd.project_init_status}`\n"
            f"Шаг: `{sdd.project_init_step or 'нет'}`\n"
            f"Тип: `{sdd.project_init_kind or 'нет'}`\n"
            f"Профиль: `{sdd.project_profile_path or 'нет'}`"
        )
        if sdd.project_init_error:
            text += f"\nОшибка: `{sdd.project_init_error}`"
        await ms.send_or_edit(query=query, chat_id=chat_id, text=text, md2=True)
        return ToolResult.ok()

    async def _cb_reset(
        self, bot_app: Any, session: Any, ms: MessagingService, chat_id: int, context: Any, query: Any
    ) -> ToolResult:
        if self._is_project_init_running(session) or self._running_sdd_task_names(session):
            await ms.send_or_edit(
                query=query,
                chat_id=chat_id,
                text="⏳ SDD выполняет задачу\\. Дождитесь завершения или выключите режим для отмены\\.",
                md2=True,
            )
            return ToolResult.ok()
        sdd = get_sdd_state(session)
        sdd.feature_slug = None
        sdd.spec_dir = None
        sdd.phase = "idle"
        sdd.pending_gate = None
        sdd.source_intent = None
        sdd.last_action = ""
        self._persist_sessions(bot_app)
        await ms.send_or_edit(query=query, chat_id=chat_id, text="🔄 SDD сброшен\\.", md2=True)
        return ToolResult.ok()

    async def _cb_init_project(
        self, bot_app: Any, session: Any, ms: MessagingService, chat_id: int, context: Any, query: Any
    ) -> ToolResult:
        sdd = get_sdd_state(session)
        if sdd.pending_gate:
            await ms.send_or_edit(
                query=query,
                chat_id=chat_id,
                text="Сначала завершите текущий SDD-гейт: принять, запросить правки или остановить фазу\\.",
                md2=True,
            )
            return ToolResult.ok()
        if sdd.last_action == "fork_analyst_running":
            await ms.send_or_edit(query=query, chat_id=chat_id, text="⏳ Аналитик уже запущен\\.", md2=True)
            return ToolResult.ok()
        if self._is_session_busy_for_sdd_action(session) or self._running_sdd_task_names(session):
            await ms.send_or_edit(
                query=query,
                chat_id=chat_id,
                text="⏳ Сессия занята\\. Дождитесь завершения текущей операции\\.",
                md2=True,
            )
            return ToolResult.ok()
        if self._is_project_init_running(session):
            await ms.send_or_edit(query=query, chat_id=chat_id, text="🧭 Инициализация проекта уже выполняется\\.", md2=True)
            return ToolResult.ok()
        workdir = str(getattr(session, "workdir", "") or "")
        if not workdir or not os.path.isdir(workdir):
            await ms.send_or_edit(query=query, chat_id=chat_id, text="❌ Рабочая директория не задана\\.", md2=True)
            return ToolResult.ok()
        try:
            classification = classify_project(workdir)
        except Exception:
            self._log.exception("sdd project init classify failed")
            await ms.send_or_edit(query=query, chat_id=chat_id, text="❌ Не удалось классифицировать проект\\.", md2=True)
            return ToolResult.ok()
        sdd.project_init_status = "confirming"
        sdd.project_init_step = "confirming"
        sdd.project_init_kind = classification.kind
        sdd.project_init_error = ""
        self._persist_sessions(bot_app)
        kind_text = "кодовая база найдена" if classification.is_existing_codebase else "кодовая база не найдена"
        details = (
            "Сначала будет актуализирован code map, затем будут созданы SDD-артефакты\\."
            if classification.is_existing_codebase
            else "Будут созданы шаблоны SDD-артефактов без запуска code map\\."
        )
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "✅ Запустить",
                        callback_data=build_mode_action_callback_data(
                            self.mode_id, "init_project_confirm", session=session
                        ),
                    )
                ],
                [
                    InlineKeyboardButton(
                        "❌ Отмена",
                        callback_data=build_mode_action_callback_data(
                            self.mode_id, "init_project_cancel", session=session
                        ),
                    )
                ],
            ]
        )
        await ms.send_or_edit(
            query=query,
            chat_id=chat_id,
            text=f"🧭 Инициализация проекта\n\nТип: {kind_text}\\.\n{details}",
            md2=True,
            reply_markup=keyboard,
        )
        return ToolResult.ok()

    async def _cb_init_project_confirm(
        self, bot_app: Any, session: Any, ms: MessagingService, chat_id: int, context: Any, query: Any
    ) -> ToolResult:
        sdd = get_sdd_state(session)
        if str(sdd.project_init_status or "").strip() != "confirming":
            await self._rerender_menu_common(
                bot_app=bot_app,
                session=session,
                chat_id=chat_id,
                context=context,
                query=query,
                note="Запрос инициализации устарел. Запустите инициализацию проекта заново.",
            )
            return ToolResult.ok()
        if self._is_session_busy_for_sdd_action(session) or self._running_sdd_task_names(session):
            await ms.send_or_edit(
                query=query,
                chat_id=chat_id,
                text="⏳ Сессия занята\\. Дождитесь завершения текущей операции\\.",
                md2=True,
            )
            return ToolResult.ok()
        if self._is_project_init_running(session):
            await ms.send_or_edit(query=query, chat_id=chat_id, text="🧭 Инициализация проекта уже выполняется\\.", md2=True)
            return ToolResult.ok()
        sdd.project_init_status = "running"
        sdd.project_init_step = "queued"
        sdd.project_init_error = ""
        self._persist_sessions(bot_app)
        self._start_mode_task(
            bot_app=bot_app,
            session=session,
            coro=self._run_project_init_task(session, bot_app, context, chat_id),
            name="sdd_init_project",
        )
        await ms.send_or_edit(
            query=query,
            chat_id=chat_id,
            text="🧭 Инициализация проекта запущена\\.",
            md2=True,
            reply_markup=None,
        )
        return ToolResult.ok()

    async def _cb_init_project_cancel(
        self, bot_app: Any, session: Any, ms: MessagingService, chat_id: int, context: Any, query: Any
    ) -> ToolResult:
        sdd = get_sdd_state(session)
        if sdd.project_init_status == "confirming":
            sdd.project_init_status = "idle"
            sdd.project_init_step = ""
            sdd.project_init_kind = ""
            sdd.project_init_error = ""
            self._persist_sessions(bot_app)
        await self._rerender_menu_common(
            bot_app=bot_app,
            session=session,
            chat_id=chat_id,
            context=context,
            query=query,
            note="Инициализация проекта отменена.",
        )
        return ToolResult.ok()

    async def _run_project_init_task(self, session: Any, bot_app: Any, context: Any, chat_id: int) -> None:
        ms = self._messaging(bot_app=bot_app, context=context)
        try:
            runtime_getter = self._optional_runtime_getter()
            result = await run_project_initialization(
                session=session,
                runtime_getter=runtime_getter,
                persist=lambda: self._persist_sessions(bot_app),
            )
            files_text = "\n".join(f"- `{path}`" for path in result.created_files[:12])
            if len(result.created_files) > 12:
                files_text += f"\n- … и ещё {len(result.created_files) - 12}"
            files_text = escape_markdown_v2_all(files_text)
            profile_path = escape_markdown_v2_all(result.project_profile_path or "нет")
            await ms.send_text(
                chat_id,
                (
                    "✅ Инициализация проекта завершена\\.\n\n"
                    f"Тип: `{result.kind}`\n"
                    f"Профиль: {profile_path}\n\n"
                    f"Создано/обновлено:\n{files_text}"
                ),
                md2=True,
            )
        except asyncio.CancelledError:
            sdd = get_sdd_state(session)
            sdd.project_init_status = "cancelled"
            sdd.project_init_step = "cancelled"
            sdd.project_init_error = ""
            self._persist_sessions(bot_app)
            try:
                await ms.send_text(chat_id, "⏹ Инициализация проекта отменена\\.", md2=True)
            except Exception:
                self._log.exception("sdd project init cancel notify failed")
            raise
        except Exception as exc:
            self._log.exception("sdd project init failed")
            try:
                await ms.send_text(
                    chat_id,
                    f"❌ Инициализация проекта не завершена: `{str(exc)}`",
                    md2=True,
                )
            except Exception:
                self._log.exception("sdd project init error notify failed")

    async def _cb_gate_accept(
        self,
        bot_app: Any,
        session: Any,
        ms: MessagingService,
        chat_id: int,
        context: Any,
        query: Any,
        dest: Dict[str, Any],
    ) -> ToolResult:
        sdd = get_sdd_state(session)
        current_phase = str(sdd.phase or "")
        pending = str(sdd.pending_gate or "")

        def restore_current_gate() -> None:
            set_sdd_phase(session, current_phase)
            sdd.pending_gate = current_phase
            sdd.last_action = "gate_revise"
            self._persist_sessions(bot_app)

        # Guard: only accept if gate matches current phase
        if pending != current_phase or not pending:
            await ms.send_or_edit(
                query=query, chat_id=chat_id,
                text="Нет активного гейта для подтверждения\\.", md2=True
            )
            return ToolResult.ok()

        clear_sdd_gate(session)
        sdd.last_action = ""
        nxt = next_phase(current_phase)

        if nxt is not None:
            self._persist_sessions(bot_app)
            self._start_mode_task(
                bot_app=bot_app,
                session=session,
                coro=self._run_phase(
                    session=session,
                    bot_app=bot_app,
                    context=context,
                    dest=dest,
                    phase=nxt,
                    restore_gate_on_failure=current_phase,
                ),
                name=f"sdd_phase_{nxt}",
            )
            await ms.send_or_edit(
                query=query, chat_id=chat_id,
                text=f"✅ Фаза `{current_phase}` принята\\. Запускаю `{nxt}`\\.", md2=True
            )
        else:
            # tasks phase accepted — handoff to Manager
            gate_keyboard = self._gate_keyboard(session, self._load_prompts())
            workdir = str(getattr(session, "workdir", "") or "")
            spec_dir = normalize_spec_dir(workdir, str(sdd.spec_dir or "")) if workdir else None
            if not spec_dir:
                restore_current_gate()
                await ms.send_or_edit(
                    query=query, chat_id=chat_id,
                    text="❌ Каталог спецификации не задан или небезопасен — нечего передавать Менеджеру\\.",
                    md2=True,
                    reply_markup=gate_keyboard,
                )
                return ToolResult.ok()
            if spec_dir != str(sdd.spec_dir or ""):
                sdd.spec_dir = spec_dir
            set_sdd_phase(session, "handoff")
            self._persist_sessions(bot_app)
            await ms.send_or_edit(
                query=query, chat_id=chat_id,
                text="✅ Задачи приняты\\. Передаю Менеджеру\\.\\.\\.",
                md2=True,
            )
            tasks_md_path = os.path.join(spec_dir, "tasks.md")
            self._start_mode_task(
                bot_app=bot_app,
                session=session,
                coro=run_handoff_to_manager(
                    mode=self,
                    session=session,
                    bot_app=bot_app,
                    context=context,
                    dest=dest,
                    tasks_md_path=tasks_md_path,
                    restore_gate_on_failure=restore_current_gate,
                    restore_gate_reply_markup=gate_keyboard,
                ),
                name="sdd_handoff_manager",
            )
        return ToolResult.ok()

    async def _cb_gate_revise(
        self, bot_app: Any, session: Any, ms: MessagingService, chat_id: int, context: Any, query: Any
    ) -> ToolResult:
        sdd = get_sdd_state(session)
        sdd.last_action = "gate_revise"
        self._persist_sessions(bot_app)
        await ms.send_or_edit(
            query=query, chat_id=chat_id,
            text="✍️ Введите правки — они будут применены при перегенерации текущей фазы\\.", md2=True
        )
        return ToolResult.ok()

    async def _cb_gate_stop(
        self, bot_app: Any, session: Any, ms: MessagingService, chat_id: int, context: Any, query: Any
    ) -> ToolResult:
        sdd = get_sdd_state(session)
        sdd.last_action = ""
        clear_sdd_gate(session)
        self._persist_sessions(bot_app)
        await ms.send_or_edit(
            query=query, chat_id=chat_id,
            text="⏹ SDD остановлен\\. Состояние сохранено\\.", md2=True
        )
        return ToolResult.ok()

    # ------------------------------------------------------------------
    # Fork menu (метаоркестратор Аналитик/SDD)
    # ------------------------------------------------------------------

    def _fork_keyboard(self, session: Any) -> InlineKeyboardMarkup:
        rows = [
            [
                InlineKeyboardButton(
                    "🔍 Через Аналитика",
                    callback_data=build_mode_action_callback_data("sdd", "fork_analyst", session=session),
                ),
                InlineKeyboardButton(
                    "📐 Сразу SDD",
                    callback_data=build_mode_action_callback_data("sdd", "fork_direct", session=session),
                ),
            ],
            [
                InlineKeyboardButton("⬅️ Назад", callback_data="sess_active"),
            ],
        ]
        return InlineKeyboardMarkup(rows)

    async def _show_fork_menu(
        self,
        session: Any,
        bot_app: Any,
        context: Any,
        chat_id: int,
        dest: Dict[str, Any],
        ms: MessagingService,
    ) -> None:
        await ms.send_text(
            chat_id,
            "Выберите путь для фичи: сначала Аналитик соберёт ТЗ или сразу перейти к спецификации\\.",
            md2=True,
            reply_markup=self._fork_keyboard(session),
        )

    async def _init_and_run_specify(
        self,
        session: Any,
        bot_app: Any,
        context: Any,
        dest: Dict[str, Any],
        intent: str,
    ) -> None:
        """Аллоцирует spec_dir, инициализирует SddState и запускает фазу specify."""
        sdd = get_sdd_state(session)
        workdir = str(getattr(session, "workdir", "") or "")
        sdd.source_intent = intent
        sdd.phase = "idle"
        sdd.pending_gate = None
        # spec_dir аллоцируем только при наличии workdir; иначе _run_phase
        # уведомит пользователя об отсутствии рабочей директории (без bogus relative-пути).
        if workdir:
            slug = slugify(intent)
            sdd.feature_slug = slug
            sdd.spec_dir = allocate_spec_dir(workdir, slug)
        self._persist_sessions(bot_app)
        self._start_mode_task(
            bot_app=bot_app,
            session=session,
            coro=self._run_phase(
                session=session, bot_app=bot_app, context=context, dest=dict(dest), phase="specify"
            ),
            name="sdd_phase_specify",
        )

    async def _cb_fork_direct(
        self,
        bot_app: Any,
        session: Any,
        ms: MessagingService,
        chat_id: int,
        context: Any,
        query: Any,
        dest: Dict[str, Any],
    ) -> ToolResult:
        sdd = get_sdd_state(session)
        intent = str(sdd.source_intent or "").strip()
        if not intent:
            await ms.send_or_edit(query=query, chat_id=chat_id, text="❌ Намерение не задано\\.", md2=True)
            return ToolResult.ok()
        await ms.send_or_edit(query=query, chat_id=chat_id, text="📐 Запускаю SDD\\.\\.\\.", md2=True)
        await self._init_and_run_specify(session, bot_app, context, dest, intent)
        return ToolResult.ok()

    async def _cb_fork_analyst(
        self,
        bot_app: Any,
        session: Any,
        ms: MessagingService,
        chat_id: int,
        context: Any,
        query: Any,
        dest: Dict[str, Any],
    ) -> ToolResult:
        sdd = get_sdd_state(session)
        intent = str(sdd.source_intent or "").strip()
        if not intent:
            await ms.send_or_edit(query=query, chat_id=chat_id, text="❌ Намерение не задано\\.", md2=True)
            return ToolResult.ok()
        if sdd.last_action == "fork_analyst_running":
            await ms.send_or_edit(query=query, chat_id=chat_id, text="⏳ Аналитик уже запущен\\.", md2=True)
            return ToolResult.ok()
        sdd.last_action = "fork_analyst_running"
        self._persist_sessions(bot_app)
        await ms.send_or_edit(query=query, chat_id=chat_id, text="🔍 Запускаю Аналитика\\.\\.\\.", md2=True)
        self._start_mode_task(
            bot_app=bot_app,
            session=session,
            coro=self._run_analyst_then_specify(session, bot_app, context, dict(dest), intent),
            name="sdd_fork_analyst",
        )
        return ToolResult.ok()

    async def _run_analyst_then_specify(
        self,
        session: Any,
        bot_app: Any,
        context: Any,
        dest: Dict[str, Any],
        intent: str,
    ) -> None:
        prev_enabled = is_orchestrator_enabled(session)
        try:
            set_orchestrator_enabled(session, False)
            await self._pipeline().run_mode_pipeline(session, intent, dict(dest), context, mode_id="analyst")
            analyst_out = str(get_orchestrator_last_mode_output(session, "") or "").strip()
            sdd = get_sdd_state(session)
            if analyst_out:
                sdd.source_intent = analyst_out
            sdd.last_action = ""
            set_active_mode(session, self.mode_id)
            self._persist_sessions(bot_app)
            await self._init_and_run_specify(session, bot_app, context, dest, str(sdd.source_intent or intent))
        except asyncio.CancelledError:
            sdd = get_sdd_state(session)
            sdd.last_action = ""
            self._persist_sessions(bot_app)
            raise
        except Exception:
            self._log.exception("sdd fork_analyst failed")
            sdd = get_sdd_state(session)
            sdd.last_action = ""
            self._persist_sessions(bot_app)
            try:
                ms = self._messaging(bot_app=bot_app, context=context)
                await ms.send_text(int(dest.get("chat_id") or 0), "❌ Ошибка при запуске Аналитика. Проверьте логи.", md2=False)
            except Exception:
                self._log.exception("sdd fork_analyst notify failed")
        finally:
            # run_mode_pipeline("analyst") can leave active_mode="analyst".
            # Restore SDD only while this task still owns the active mode;
            # cancellation from on_disable clears it before task cancellation.
            current_mode = str(get_active_mode(session, "") or "")
            if current_mode in {"analyst", self.mode_id}:
                set_active_mode(session, self.mode_id)
            set_orchestrator_enabled(session, prev_enabled)
            self._persist_sessions(bot_app)

    # ------------------------------------------------------------------
    # build_menu
    # ------------------------------------------------------------------

    def build_menu(
        self,
        session: Any,
        back_callback: str = "sess_active",
        back_text: str = "⬅️ Назад",
        menu_visibility: Any = None,
    ) -> Tuple[str, Any]:
        enabled = str(get_active_mode(session, "") or "").strip() == self.mode_id
        sdd = get_sdd_state(session)
        rows = []
        if enabled:
            rows.append([
                InlineKeyboardButton(
                    "🔴 Выключить SDD",
                    callback_data=build_mode_action_callback_data(self.mode_id, "disable", session=session),
                )
            ])
            rows.append([
                InlineKeyboardButton(
                    "🧭 Инициализировать проект",
                    callback_data=build_mode_action_callback_data(self.mode_id, "init_project", session=session),
                )
            ])
            rows.append([
                InlineKeyboardButton(
                    "📊 Статус",
                    callback_data=build_mode_action_callback_data(self.mode_id, "status", session=session),
                ),
                InlineKeyboardButton(
                    "🔄 Сбросить",
                    callback_data=build_mode_action_callback_data(self.mode_id, "reset", session=session),
                ),
            ])
            text = (
                f"📐 SDD\n\nРежим: включен\n"
                f"Фаза: {sdd.phase}\n"
                f"Фича: {sdd.feature_slug or 'нет'}\n"
                f"Инициализация проекта: {sdd.project_init_status}"
            )
        else:
            rows.append([
                InlineKeyboardButton(
                    "🟢 Включить SDD",
                    callback_data=build_mode_action_callback_data(self.mode_id, "enable", session=session),
                )
            ])
            text = "📐 SDD\n\nРежим: выключен\n\nSpec-Driven Development: specify → plan → tasks."
        rows.append([InlineKeyboardButton(back_text, callback_data=back_callback)])
        return text, InlineKeyboardMarkup(rows)

    def _is_project_init_running(self, session: Any) -> bool:
        sdd = get_sdd_state(session)
        if str(sdd.project_init_status or "").strip() == "running":
            try:
                return "sdd_init_project" in self._mode_task_names(bot_app=None, session=session)
            except Exception:
                return True
        try:
            return "sdd_init_project" in self._mode_task_names(bot_app=None, session=session)
        except Exception:
            return False

    def _running_sdd_task_names(self, session: Any) -> Tuple[str, ...]:
        try:
            names = self._mode_task_names(bot_app=None, session=session)
        except Exception:
            return ()
        running: list[str] = []
        for name in names:
            text = str(name or "")
            if text == "sdd_init_project" or text == "sdd_handoff_manager" or text.startswith("sdd_phase_"):
                running.append(text)
        return tuple(running)

    @staticmethod
    def _is_session_busy_for_sdd_action(session: Any) -> bool:
        run_lock = getattr(session, "run_lock", None)
        queue_len = len(getattr(session, "queue", []) or [])
        return bool(is_session_busy(session, run_lock) or queue_len > 0)

    # ------------------------------------------------------------------
    # on_enable / on_disable
    # ------------------------------------------------------------------

    async def on_enable(self, ctx: Dict[str, Any]) -> Optional[ToolResult]:
        session = ctx.get("session")
        bot_app = ctx.get("bot_app")
        if session and bot_app:
            error_text = self._enable_requirements_error(bot_app, session)
            if error_text:
                return ToolResult.fail("requirements_not_met", output=error_text)
            await self._activate_mode(session=session, bot_app=bot_app)
        return None

    async def on_disable(self, ctx: Dict[str, Any]) -> Optional[ToolResult]:
        session = ctx.get("session")
        bot_app = ctx.get("bot_app")
        if session and bot_app and self._is_mode_active(session):
            await self._deactivate_mode(session=session, bot_app=bot_app, cancel_tasks=True, timeout_s=0.2)
        return None
