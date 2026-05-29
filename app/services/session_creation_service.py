import logging
import os
import re
from typing import Optional

from app.services.actor_identity import telegram_actor_id
from app.services.project_registry import ProjectConflictError, ProjectOwnershipError
from app.services.telegram_ui_scope import TelegramUiKey


logger = logging.getLogger(__name__)


class SessionCreationService:
    """Centralized new-session creation flow (tool selection, dirs pick, git clone)."""

    def __init__(self, bot_app):
        self.bot_app = bot_app

    def _ui_key(self, chat_id: int, *, message_thread_id: Optional[int] = None) -> TelegramUiKey:
        return self.bot_app.telegram_ui_key(int(chat_id), message_thread_id)

    def _effective_ui_key(
        self,
        owner_chat_id: int,
        *,
        ui_chat_id: Optional[int] = None,
        message_thread_id: Optional[int] = None,
    ) -> TelegramUiKey:
        key_chat_id = int(ui_chat_id) if ui_chat_id is not None else int(owner_chat_id)
        return self._ui_key(key_chat_id, message_thread_id=message_thread_id)

    def validate_tool(self, tool: str) -> Optional[str]:
        if tool not in self.bot_app.config.tools:
            return "Инструмент не найден."
        if not self.bot_app._is_tool_available(tool):
            return (
                "Инструмент не установлен. Сначала установите его. "
                f"Ожидаемые: {self.bot_app._expected_tools()}"
            )
        return None

    def begin_new_session_flow(
        self,
        owner_chat_id: int,
        tool: str,
        *,
        message_thread_id: Optional[int] = None,
        ui_chat_id: Optional[int] = None,
    ) -> Optional[str]:
        err = self.validate_tool(str(tool))
        if err:
            return err
        ui_key = self._effective_ui_key(owner_chat_id, ui_chat_id=ui_chat_id, message_thread_id=message_thread_id)
        self.bot_app.ui_state.pending_new_tool[ui_key] = str(tool)
        return None

    @staticmethod
    def _normalize_workdir(path: str) -> str:
        return str(path or "").strip()

    def _validate_workdir(self, path: str, *, root: Optional[str] = None) -> tuple[Optional[str], Optional[str]]:
        workdir = self._normalize_workdir(path)
        if not os.path.isdir(workdir):
            return None, "Каталог не существует."
        if root and not self.bot_app.is_within_root(workdir, root):
            return None, "Нельзя выйти за пределы корневого каталога."
        return workdir, None

    async def create_session(
        self,
        owner_chat_id: int,
        tool: Optional[str],
        path: str,
        *,
        root: Optional[str] = None,
        bot=None,
        ui_chat_id: Optional[int] = None,
        register_project: bool = False,
    ):
        _ = ui_chat_id
        return await self._create_authoritative_session(
            owner_chat_id=int(owner_chat_id),
            tool=tool,
            path=path,
            root=root,
            bot=bot,
            register_project=bool(register_project),
        )

    def register_project(
        self,
        owner_chat_id: int,
        path: str,
        *,
        name: Optional[str] = None,
    ) -> Optional[str]:
        registry = getattr(self.bot_app, "project_registry", None)
        if registry is None:
            return None
        owner_id = telegram_actor_id(owner_chat_id)
        try:
            registry.register_project(path=str(path), owner_id=owner_id, name=name)
        except ProjectOwnershipError:
            return "Проект уже принадлежит другому пользователю."
        except ProjectConflictError:
            return "Проект с таким идентификатором уже существует."
        except Exception:
            logger.exception("project registration failed owner_chat_id=%s path=%s", owner_chat_id, path)
            return "Не удалось зарегистрировать проект."
        return None

    async def create_from_pending_tool(
        self,
        owner_chat_id: int,
        path: str,
        *,
        root: Optional[str] = None,
        clear_dirs_mode: bool = False,
        bot=None,
        message_thread_id: Optional[int] = None,
        ui_chat_id: Optional[int] = None,
    ):
        ui_key = self._effective_ui_key(owner_chat_id, ui_chat_id=ui_chat_id, message_thread_id=message_thread_id)
        tool = self.bot_app.ui_state.pending_new_tool.pop(ui_key, None)
        if not tool:
            return None, "Инструмент не выбран."
        return await self._create_authoritative_session(
            owner_chat_id=int(owner_chat_id),
            tool=str(tool),
            path=path,
            root=root,
            bot=bot,
            register_project=True,
            clear_dirs_mode=bool(clear_dirs_mode),
            ui_key=ui_key,
        )

    def mark_git_clone_pending(
        self,
        owner_chat_id: int,
        base: str,
        *,
        message_thread_id: Optional[int] = None,
        ui_chat_id: Optional[int] = None,
    ) -> None:
        ui_key = self._effective_ui_key(owner_chat_id, ui_chat_id=ui_chat_id, message_thread_id=message_thread_id)
        self.bot_app.ui_state.pending_git_clone[ui_key] = str(base)

    def pop_git_clone_pending(
        self,
        owner_chat_id: int,
        *,
        message_thread_id: Optional[int] = None,
        ui_chat_id: Optional[int] = None,
    ) -> Optional[str]:
        ui_key = self._effective_ui_key(owner_chat_id, ui_chat_id=ui_chat_id, message_thread_id=message_thread_id)
        return self.bot_app.ui_state.pending_git_clone.pop(ui_key, None)

    async def complete_git_clone(
        self,
        *,
        owner_chat_id: int,
        base: str,
        url: str,
        output: str,
        bot=None,
        message_thread_id: Optional[int] = None,
        ui_chat_id: Optional[int] = None,
    ):
        ui_key = self._effective_ui_key(owner_chat_id, ui_chat_id=ui_chat_id, message_thread_id=message_thread_id)
        tool = self.bot_app.ui_state.pending_new_tool.pop(ui_key, None)
        if not tool:
            return None, None
        repo_path = self._extract_clone_path(base=base, output=output, url=url)
        root = self.bot_app.ui_state.dirs_root.get(ui_key, self.bot_app.config.defaults.workdir)
        if repo_path and os.path.isdir(repo_path) and self.bot_app.is_within_root(repo_path, root):
            return await self._create_authoritative_session(
                owner_chat_id=int(owner_chat_id),
                tool=str(tool),
                path=repo_path,
                root=root,
                bot=bot,
                register_project=True,
                clear_dirs_mode=True,
                ui_key=ui_key,
            )
        return None, None

    async def _create_authoritative_session(
        self,
        *,
        owner_chat_id: int,
        tool: Optional[str],
        path: str,
        root: Optional[str] = None,
        bot=None,
        register_project: bool = False,
        clear_dirs_mode: bool = False,
        ui_key: Optional[TelegramUiKey] = None,
    ):
        tool_name = str(tool or "").strip() or None
        if tool_name is not None:
            err = self.validate_tool(tool_name)
            if err:
                return None, err
        workdir, path_error = self._validate_workdir(path, root=root)
        if path_error:
            return None, path_error
        assert workdir is not None

        session = self.bot_app.manager.create(int(owner_chat_id), tool_name, workdir)
        access_policy = getattr(self.bot_app, "access_policy_service", None)
        applied_default_mode_id = None
        if access_policy is not None and hasattr(access_policy, "apply_default_mode_for_session"):
            applied_default_mode_id = access_policy.apply_default_mode_for_session(
                session,
                chat_id=int(owner_chat_id),
            )
        if applied_default_mode_id:
            persist_session = getattr(self.bot_app.manager, "persist_session", None)
            try:
                if callable(persist_session):
                    persist_session(int(owner_chat_id), session.id)
                else:
                    self.bot_app.manager._persist_sessions()
            except Exception:
                logger.exception(
                    "session creation failed to persist default mode owner_chat_id=%s session_id=%s mode_id=%s",
                    owner_chat_id,
                    getattr(session, "id", ""),
                    applied_default_mode_id,
                )
        topic_error = await self._bind_session_topic(int(owner_chat_id), session, bot=bot)
        if topic_error:
            return None, topic_error

        if register_project:
            registry_error = self.register_project(int(owner_chat_id), workdir)
            if registry_error:
                await self._rollback_created_session(int(owner_chat_id), session, bot=bot)
                return None, registry_error

        if clear_dirs_mode and ui_key is not None:
            self.bot_app.ui_state.dirs_mode.pop(ui_key, None)
        return session, None

    async def _rollback_created_session(self, owner_chat_id: int, session, *, bot=None) -> None:
        session_id = str(getattr(session, "id", "") or "")
        try:
            close_headless_process = getattr(session, "close_headless_process", None)
            if callable(close_headless_process):
                close_headless_process()
        except Exception:
            logger.exception(
                "session rollback headless abort failed owner_chat_id=%s session_id=%s",
                owner_chat_id,
                session_id,
            )

        thread_manager = getattr(self.bot_app, "session_thread_manager", None)
        cleanup_closed_session = getattr(thread_manager, "cleanup_closed_session", None)
        if callable(cleanup_closed_session):
            try:
                await cleanup_closed_session(
                    owner_chat_id=int(owner_chat_id),
                    session_id=session_id,
                    bot=bot,
                    scope=getattr(session, "conversation_scope", None),
                )
            except Exception:
                logger.exception(
                    "session rollback topic cleanup failed owner_chat_id=%s session_id=%s",
                    owner_chat_id,
                    session_id,
                )

        try:
            self.bot_app.manager.close(int(owner_chat_id), session_id)
        except Exception:
            logger.exception(
                "session rollback close failed owner_chat_id=%s session_id=%s",
                owner_chat_id,
                session_id,
            )

    async def _bind_session_topic(self, owner_chat_id: int, session, *, bot=None) -> Optional[str]:
        thread_manager = getattr(self.bot_app, "session_thread_manager", None)
        if thread_manager is None or not thread_manager.is_enabled():
            return None
        try:
            await thread_manager.ensure_topic_for_session(
                owner_chat_id=int(owner_chat_id),
                session=session,
                bot=bot,
            )
            return None
        except Exception:
            logger.exception(
                "session topic binding failed owner_chat_id=%s session_id=%s",
                owner_chat_id,
                getattr(session, "id", None),
            )
            await self._rollback_created_session(int(owner_chat_id), session, bot=bot)
            return (
                "Не удалось создать topic для сессии. "
                "Проверьте, что у бота есть право управлять темами форума."
            )

    def _extract_clone_path(self, *, base: str, output: str, url: str) -> Optional[str]:
        repo_path = None
        match = re.search(r"Cloning into '([^']+)'", str(output or ""))
        if match:
            repo_path = os.path.join(str(base), match.group(1))
        if not repo_path:
            repo_path = self.bot_app._guess_clone_path(str(url), str(base))
        return repo_path
