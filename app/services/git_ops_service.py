import asyncio
import html
import os
import tempfile
import logging
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from session import Session, SessionManager
from utils.html_renderer import make_html_file


class GitOps:
    def __init__(
        self,
        config,
        manager: SessionManager,
        send_message,
        edit_message,
        send_document,
        short_label,
        handle_cli_input,
    ) -> None:
        self.config = config
        self.manager = manager
        self._send_message = send_message
        self._edit_message = edit_message
        self._send_document = send_document
        self._short_label = short_label
        self._handle_cli_input = handle_cli_input
        self.git_branch_menu: dict[tuple[int, Optional[int]], list] = {}
        self.git_pending_ref: dict[tuple[int, Optional[int]], str] = {}
        self.git_pull_target: dict[tuple[int, Optional[int]], str] = {}
        self.pending_git_commit: dict[tuple[int, Optional[int]], str] = {}
        self._git_askpass_path: Optional[str] = None

    @staticmethod
    def _state_key(chat_id: int, message_thread_id: Optional[int] = None) -> tuple[int, Optional[int]]:
        thread_id = int(message_thread_id) if message_thread_id is not None else None
        return int(chat_id), thread_id

    @staticmethod
    def _send_kwargs(chat_id: int, *, message_thread_id: Optional[int] = None) -> dict:
        kwargs = {"chat_id": int(chat_id)}
        if message_thread_id is not None:
            kwargs["message_thread_id"] = int(message_thread_id)
        return kwargs

    @staticmethod
    def _query_thread_id(query) -> Optional[int]:
        raw_value = getattr(getattr(query, "message", None), "message_thread_id", None)
        try:
            return int(raw_value) if raw_value is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _resolve_git_help_path() -> str:
        service_dir = os.path.dirname(__file__)
        project_root = os.path.abspath(os.path.join(service_dir, "..", ".."))
        return os.path.join(project_root, "git.md")

    async def _edit_msg(self, context: ContextTypes.DEFAULT_TYPE, query, text: str, *, reply_markup=None) -> bool:
        if not query.message:
            return False
        return await self._edit_message(
            context,
            chat_id=query.message.chat_id,
            message_id=query.message.message_id,
            text=text,
            md2=True,
            reply_markup=reply_markup,
        )

    def _ensure_git_askpass(self) -> Optional[str]:
        token = self.config.defaults.github_token
        if not token:
            return None
        if self._git_askpass_path and os.path.isfile(self._git_askpass_path):
            return self._git_askpass_path
        fd, path = tempfile.mkstemp(prefix="cli-proxy-git-askpass-", text=True)
        script = (
            "#!/bin/sh\n"
            "prompt=\"$1\"\n"
            "case \"$prompt\" in\n"
            "*Username*) echo \"x-access-token\" ;;\n"
            "*Password*) echo \"$GIT_ASKPASS_TOKEN\" ;;\n"
            "*) echo \"$GIT_ASKPASS_TOKEN\" ;;\n"
            "esac\n"
        )
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(script)
        os.chmod(path, 0o700)
        self._git_askpass_path = path
        return path

    def git_env(self) -> dict:
        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"
        token = self.config.defaults.github_token
        if token:
            askpass = self._ensure_git_askpass()
            if askpass:
                env["GIT_ASKPASS"] = askpass
                env["GIT_ASKPASS_TOKEN"] = token
                env["GIT_USERNAME"] = "x-access-token"
        return env

    def build_git_keyboard(self) -> InlineKeyboardMarkup:
        rows = [
            [
                InlineKeyboardButton("📋 Status", callback_data="git_status"),
                InlineKeyboardButton("📡 Fetch", callback_data="git_fetch"),
            ],
            [
                InlineKeyboardButton("⬇️ Pull", callback_data="git_pull"),
                InlineKeyboardButton("🔀 Merge", callback_data="git_merge_menu"),
            ],
            [
                InlineKeyboardButton("🔀 Rebase", callback_data="git_rebase_menu"),
                InlineKeyboardButton("📝 Diff", callback_data="git_diff"),
            ],
            [
                InlineKeyboardButton("📜 Log", callback_data="git_log"),
                InlineKeyboardButton("📦 Stash", callback_data="git_stash"),
            ],
            [
                InlineKeyboardButton("💾 Commit", callback_data="git_commit"),
                InlineKeyboardButton("⬆️ Push", callback_data="git_push"),
            ],
            [
                InlineKeyboardButton("📊 Summary", callback_data="git_summary"),
            ],
            [
                InlineKeyboardButton("❓ Help", callback_data="git_help"),
            ],
            [
                InlineKeyboardButton("❌ Закрыть", callback_data="git_cancel"),
            ],
        ]
        return InlineKeyboardMarkup(rows)

    def _build_git_branches_keyboard(
        self,
        chat_id: int,
        action: str,
        *,
        message_thread_id: Optional[int] = None,
    ) -> InlineKeyboardMarkup:
        branches = self.git_branch_menu.get(self._state_key(chat_id, message_thread_id), [])
        rows = []
        for i, ref in enumerate(branches):
            rows.append(
                [InlineKeyboardButton(self._short_label(ref), callback_data=f"git_{action}_pick:{i}")]
            )
        rows.append([InlineKeyboardButton("❌ Отмена", callback_data="git_cancel")])
        return InlineKeyboardMarkup(rows)

    def _build_git_pull_keyboard(self, ref: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(f"🔀 Merge {ref}", callback_data="git_pull_merge"),
                    InlineKeyboardButton(f"🔀 Rebase {ref}", callback_data="git_pull_rebase"),
                ],
                [InlineKeyboardButton("❌ Отмена", callback_data="git_pull_cancel")],
            ]
        )

    def _build_git_confirm_keyboard(self, action: str, ref: str) -> InlineKeyboardMarkup:
        label = "✅ Выполнить merge" if action == "merge" else "✅ Выполнить rebase"
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton(f"{label} {ref}", callback_data=f"git_confirm_{action}")],
                [InlineKeyboardButton("❌ Отмена", callback_data="git_cancel")],
            ]
        )

    def _build_git_conflict_keyboard(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("📝 Diff", callback_data="git_conflict_diff"),
                    InlineKeyboardButton("⛔ Abort", callback_data="git_conflict_abort"),
                ],
                [
                    InlineKeyboardButton("▶️ Continue", callback_data="git_conflict_continue"),
                    InlineKeyboardButton("🤖 Позвать агента", callback_data="git_conflict_agent"),
                ],
                [
                    InlineKeyboardButton("❌ Закрыть", callback_data="git_cancel"),
                ],
            ]
        )

    def _ensure_git_state(self, session: Session) -> None:
        git_state = getattr(session, "git", None)
        if git_state is None:
            return
        if not hasattr(git_state, "busy"):
            git_state.busy = False
        if not hasattr(git_state, "conflict"):
            git_state.conflict = False
        if not hasattr(git_state, "conflict_files"):
            git_state.conflict_files = []
        if not hasattr(git_state, "conflict_kind"):
            git_state.conflict_kind = None
        # Guard: восстанавливаем lock при десериализации из состояния
        if not hasattr(git_state, "lock"):
            git_state.lock = asyncio.Lock()

    async def _try_acquire_git_busy(
        self,
        session: Session,
        chat_id: int,
        context: ContextTypes.DEFAULT_TYPE,
        *,
        message_thread_id: Optional[int] = None,
    ) -> bool:
        """Атомарный check-and-set для session.git.busy.

        Возвращает True и выставляет busy=True, если операцию можно начать.
        Возвращает False и отправляет сообщение об ошибке в противном случае.
        """
        self._ensure_git_state(session)
        if session.busy or session.is_active_by_tick():
            await self._send_git_message(
                context,
                chat_id,
                session,
                "CLI-сессия занята. Дождитесь завершения и попробуйте снова.",
                message_thread_id=message_thread_id,
            )
            return False
        git_lock = getattr(session.git, "lock", None)
        if git_lock is None:
            # Fallback: lock недоступен — используем простую проверку без атомарности
            if session.git.busy:
                await self._send_git_message(
                    context,
                    chat_id,
                    session,
                    "Git уже выполняется. Дождитесь завершения.",
                    message_thread_id=message_thread_id,
                )
                return False
            session.git.busy = True
            return True
        async with git_lock:
            if session.git.busy:
                await self._send_git_message(
                    context,
                    chat_id,
                    session,
                    "Git уже выполняется. Дождитесь завершения.",
                    message_thread_id=message_thread_id,
                )
                return False
            session.git.busy = True
        return True

    def _resolve_scope_session(self, chat_id: int, *, message_thread_id: Optional[int] = None) -> Optional[Session]:
        resolver = getattr(self.manager, "get_by_scope", None)
        if callable(resolver):
            try:
                session = resolver(int(chat_id), message_thread_id)
            except Exception:
                logging.getLogger(__name__).exception(
                    "git session scope resolution failed chat_id=%s message_thread_id=%s",
                    chat_id,
                    message_thread_id,
                )
                session = None
            if session is not None:
                return session
        return None

    async def ensure_git_session(
        self,
        chat_id: int,
        context: ContextTypes.DEFAULT_TYPE,
        *,
        message_thread_id: Optional[int] = None,
    ) -> Optional[Session]:
        session = self._resolve_scope_session(int(chat_id), message_thread_id=message_thread_id)
        if not session:
            await self._send_message(
                context,
                text="Сессия для текущего контекста не найдена. Откройте /sessions и выберите нужную сессию явно.",
                **self._send_kwargs(chat_id, message_thread_id=message_thread_id),
            )
            return None
        self._ensure_git_state(session)
        return session

    def _session_label(self, session: Session) -> str:
        label = session.name or f"{session.tool.name} @ {session.workdir}"
        return f"сессия: {session.id} | {label}"

    async def _send_git_message(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        session: Session,
        text: str,
        *,
        message_thread_id: Optional[int] = None,
    ) -> None:
        prefix = self._session_label(session)
        await self._send_message(
            context,
            text=f"{prefix}\n{text}",
            **self._send_kwargs(chat_id, message_thread_id=message_thread_id),
        )

    async def ensure_git_repo(
        self,
        session: Session,
        chat_id: int,
        context: ContextTypes.DEFAULT_TYPE,
        *,
        message_thread_id: Optional[int] = None,
    ) -> bool:
        code, output = await self._run_git(session, ["rev-parse", "--is-inside-work-tree"])
        if code != 0 or output.strip() != "true":
            await self._send_git_message(
                context,
                chat_id,
                session,
                "Каталог не является git-репозиторием.",
                message_thread_id=message_thread_id,
            )
            return False
        return True

    async def ensure_git_not_busy(
        self,
        session: Session,
        chat_id: int,
        context: ContextTypes.DEFAULT_TYPE,
        *,
        message_thread_id: Optional[int] = None,
    ) -> bool:
        self._ensure_git_state(session)
        if session.busy or session.is_active_by_tick():
            await self._send_git_message(
                context,
                chat_id,
                session,
                "CLI-сессия занята. Дождитесь завершения и попробуйте снова.",
                message_thread_id=message_thread_id,
            )
            return False
        if session.git.busy:
            await self._send_git_message(
                context,
                chat_id,
                session,
                "Git уже выполняется. Дождитесь завершения.",
                message_thread_id=message_thread_id,
            )
            return False
        return True

    async def _run_git(self, session: Session, args: list[str]) -> tuple[int, str]:
        env = self.git_env()
        env["GIT_PAGER"] = "cat"
        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=session.workdir,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate()
        output = (out or b"").decode(errors="ignore")
        return proc.returncode or 0, output

    async def _git_current_branch(self, session: Session) -> Optional[str]:
        code, output = await self._run_git(session, ["rev-parse", "--abbrev-ref", "HEAD"])
        if code != 0:
            return None
        return output.strip() or None

    async def _git_upstream(self, session: Session) -> Optional[str]:
        code, output = await self._run_git(session, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])
        if code != 0:
            return None
        return output.strip() or None

    async def _git_ref_exists(self, session: Session, ref: str) -> bool:
        code, _ = await self._run_git(session, ["rev-parse", "--verify", "--quiet", ref])
        return code == 0

    async def _git_default_remote(self, session: Session) -> Optional[str]:
        code, output = await self._run_git(session, ["symbolic-ref", "--short", "refs/remotes/origin/HEAD"])
        if code != 0:
            for ref in ("origin/main", "origin/master"):
                if await self._git_ref_exists(session, ref):
                    return ref
            return None
        ref = output.strip()
        if ref.startswith("origin/"):
            return ref
        return None

    async def _git_ahead_behind(self, session: Session, ref: str) -> Optional[tuple[int, int]]:
        code, output = await self._run_git(session, ["rev-list", "--left-right", "--count", f"HEAD...{ref}"])
        if code != 0:
            return None
        parts = output.strip().split()
        if len(parts) != 2:
            return None
        ahead = int(parts[0])
        behind = int(parts[1])
        return ahead, behind

    async def _git_in_progress(self, session: Session) -> Optional[str]:
        code, output = await self._run_git(session, ["rev-parse", "--git-path", "rebase-apply"])
        if code == 0 and output.strip():
            return "rebase"
        code, output = await self._run_git(session, ["rev-parse", "--git-path", "rebase-merge"])
        if code == 0 and output.strip():
            return "rebase"
        code, output = await self._run_git(session, ["rev-parse", "--git-path", "MERGE_HEAD"])
        if code == 0 and output.strip():
            return "merge"
        return None

    def _git_set_conflict(self, session: Session, files: list[str], kind: Optional[str]) -> None:
        session.git.conflict = True
        session.git.conflict_files = files
        session.git.conflict_kind = kind

    def _git_clear_conflict(self, session: Session) -> None:
        session.git.conflict = False
        session.git.conflict_files = []
        session.git.conflict_kind = None

    async def _git_conflict_files(self, session: Session) -> list[str]:
        code, output = await self._run_git(session, ["diff", "--name-only", "--diff-filter=U"])
        if code != 0:
            self._git_clear_conflict(session)
            return []
        files = [line.strip() for line in output.splitlines() if line.strip()]
        if files:
            kind = await self._git_in_progress(session)
            self._git_set_conflict(session, files, kind)
        else:
            self._git_clear_conflict(session)
        return files

    async def git_branch_create(self, session: Session, branch_name: str) -> tuple[int, str]:
        """Создаёт новую ветку и переключается на неё."""
        return await self._run_git(session, ["checkout", "-b", branch_name])

    async def git_checkout(self, session: Session, branch_name: str) -> tuple[int, str]:
        """Переключается на существующую ветку."""
        return await self._run_git(session, ["checkout", branch_name])

    async def git_stash_pop(self, session: Session) -> tuple[int, str]:
        """Применяет последний stash."""
        return await self._run_git(session, ["stash", "pop"])

    async def git_show(self, session: Session, ref: str = "HEAD") -> tuple[int, str]:
        """Показывает diff и мета-информацию коммита."""
        return await self._run_git(session, ["--no-pager", "show", "--stat", ref])

    async def _git_status_text(self, session: Session) -> str:
        branch = await self._git_current_branch(session) or "неизвестно"
        code, output = await self._run_git(session, ["status", "--porcelain"])
        dirty = bool(output.strip()) if code == 0 else False
        upstream = await self._git_upstream(session)
        if not upstream and branch and branch != "HEAD":
            candidate = f"origin/{branch}"
            if await self._git_ref_exists(session, candidate):
                upstream = candidate
        if not upstream:
            upstream = await self._git_default_remote(session)
        ahead_behind = await self._git_ahead_behind(session, upstream) if upstream else None
        conflicts = await self._git_conflict_files(session)
        lines = [
            f"Ветка: {branch}",
            f"Состояние: {'dirty' if dirty else 'clean'}",
        ]
        if upstream and ahead_behind:
            ahead, behind = ahead_behind
            lines.append(f"Upstream: {upstream} | ahead {ahead} / behind {behind}")
        elif upstream:
            lines.append(f"Upstream: {upstream} | ahead/behind: недоступно")
        else:
            lines.append("Upstream: нет")
        if conflicts:
            lines.append(f"Конфликт: да ({len(conflicts)} файлов)")
        else:
            lines.append("Конфликт: нет")
        return "\n".join(lines)

    async def _send_git_help(
        self,
        session: Session,
        chat_id: int,
        context: ContextTypes.DEFAULT_TYPE,
        *,
        message_thread_id: Optional[int] = None,
    ) -> None:
        path = self._resolve_git_help_path()
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read().strip()
        except Exception as e:
            logging.exception(f"tool failed {str(e)}")
            await self._send_git_message(
                context,
                chat_id,
                session,
                f"Не удалось открыть git.md: {e}",
                message_thread_id=message_thread_id,
            )
            return
        if not content:
            await self._send_git_message(
                context,
                chat_id,
                session,
                "git.md пустой.",
                message_thread_id=message_thread_id,
            )
            return
        html_text = f"<pre>{html.escape(content)}</pre>"
        out_path = make_html_file(html_text, "git-help")
        try:
            await self._send_git_message(
                context,
                chat_id,
                session,
                "Git help:",
                message_thread_id=message_thread_id,
            )
            with open(out_path, "rb") as f:
                await self._send_document(
                    context,
                    document=f,
                    **self._send_kwargs(chat_id, message_thread_id=message_thread_id),
                )
        finally:
            try:
                os.remove(out_path)
            except Exception:
                pass

    async def _git_commit_context(self, session: Session) -> Optional[str]:
        code, status_out = await self._run_git(session, ["status", "--porcelain"])
        if code != 0:
            return None
        code, stat_out = await self._run_git(session, ["diff", "--stat"])
        if code != 0:
            stat_out = ""
        code, diff_out = await self._run_git(session, ["diff"])
        if code != 0:
            diff_out = ""
        text = (
            "git status --porcelain:\n"
            f"{status_out.strip()}\n\n"
            "git diff --stat:\n"
            f"{stat_out.strip()}\n\n"
            "git diff:\n"
            f"{diff_out.strip()}"
        )
        return text.strip()

    def _sanitize_commit_message(self, message: str, max_len: int = 100) -> str:
        cleaned = message.splitlines()[0].strip()
        if len(cleaned) > max_len:
            cleaned = cleaned[:max_len].rstrip()
        return cleaned

    def _sanitize_commit_body(self, body: str, max_len: int = 2000) -> str:
        cleaned = body.strip()
        if len(cleaned) > max_len:
            cleaned = cleaned[:max_len].rstrip()
        return cleaned

    async def _build_commit_body(self, session: Session) -> Optional[str]:
        code, stat_out = await self._run_git(session, ["diff", "--stat"])
        if code != 0:
            stat_out = ""
        code, status_out = await self._run_git(session, ["status", "--porcelain"])
        if code != 0:
            status_out = ""
        parts = []
        if stat_out.strip():
            parts.append("Изменения:\n" + stat_out.strip())
        if status_out.strip():
            parts.append("Статус:\n" + status_out.strip())
        if not parts:
            return None
        return "\n\n".join(parts)

    async def _send_git_output(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        session: Session,
        title: str,
        output: str,
        *,
        message_thread_id: Optional[int] = None,
    ) -> None:
        text = output.strip()
        if not text:
            await self._send_git_message(
                context,
                chat_id,
                session,
                f"{title}: готово.",
                message_thread_id=message_thread_id,
            )
            return
        if len(text) > 4000:
            text = text[:4000]
        await self._send_git_message(
            context,
            chat_id,
            session,
            f"{title}:\n{text}",
            message_thread_id=message_thread_id,
        )

    async def _execute_git_commit(
        self,
        session: Session,
        chat_id: int,
        context: ContextTypes.DEFAULT_TYPE,
        message: str,
        body: Optional[str] = None,
        *,
        message_thread_id: Optional[int] = None,
    ) -> None:
        if not await self._try_acquire_git_busy(session, chat_id, context, message_thread_id=message_thread_id):
            return
        try:
            code, add_out = await self._run_git(session, ["add", "-A"])
            if code != 0:
                await self._send_git_output(
                    context,
                    chat_id,
                    session,
                    "Git add",
                    add_out,
                    message_thread_id=message_thread_id,
                )
                return
            args = ["commit", "-m", message]
            if body:
                args += ["-m", body]
            code, commit_out = await self._run_git(session, args)
            await self._send_git_output(
                context,
                chat_id,
                session,
                "Git commit",
                commit_out,
                message_thread_id=message_thread_id,
            )
            if code == 0:
                status = await self._git_status_text(session)
                await self._send_git_message(
                    context,
                    chat_id,
                    session,
                    status,
                    message_thread_id=message_thread_id,
                )
        finally:
            session.git.busy = False

    async def _handle_git_conflict(
        self,
        session: Session,
        chat_id: int,
        context: ContextTypes.DEFAULT_TYPE,
        *,
        message_thread_id: Optional[int] = None,
    ) -> None:
        files = session.git.conflict_files or await self._git_conflict_files(session)
        files_text = ", ".join(files[:10]) if files else "нет файлов"
        text = f"Обнаружены git-конфликты: {files_text}"
        prefix = self._session_label(session)
        await self._send_message(
            context,
            text=f"{prefix}\n{text}",
            reply_markup=self._build_git_conflict_keyboard(),
            **self._send_kwargs(chat_id, message_thread_id=message_thread_id),
        )

    async def _git_merge_or_rebase(
        self,
        session: Session,
        chat_id: int,
        context: ContextTypes.DEFAULT_TYPE,
        action: str,
        ref: str,
        *,
        message_thread_id: Optional[int] = None,
    ) -> None:
        if not await self._try_acquire_git_busy(session, chat_id, context, message_thread_id=message_thread_id):
            return
        try:
            code, output = await self._run_git(session, [action, ref])
            await self._send_git_output(
                context,
                chat_id,
                session,
                f"{action.title()} {ref}",
                output,
                message_thread_id=message_thread_id,
            )
            conflicts = await self._git_conflict_files(session)
            if conflicts:
                await self._handle_git_conflict(
                    session,
                    chat_id,
                    context,
                    message_thread_id=message_thread_id,
                )
            else:
                self._git_clear_conflict(session)
        finally:
            session.git.busy = False

    async def handle_pending_commit_message(
        self,
        chat_id: int,
        text: str,
        context: ContextTypes.DEFAULT_TYPE,
        *,
        message_thread_id: Optional[int] = None,
    ) -> bool:
        state_key = self._state_key(chat_id, message_thread_id)
        if state_key not in self.pending_git_commit:
            return False
        session_id = self.pending_git_commit.pop(state_key)
        message = text.strip()
        if message in ("-", "отмена", "Отмена"):
            session = self.manager.get(chat_id, session_id)
            if session:
                await self._send_git_message(
                    context,
                    chat_id,
                    session,
                    "Коммит отменен.",
                    message_thread_id=message_thread_id,
                )
            else:
                await self._send_message(
                    context,
                    text="Коммит отменен.",
                    **self._send_kwargs(chat_id, message_thread_id=message_thread_id),
                )
            return True
        if not message:
            session = self.manager.get(chat_id, session_id)
            if session:
                await self._send_git_message(
                    context,
                    chat_id,
                    session,
                    "Сообщение коммита пустое.",
                    message_thread_id=message_thread_id,
                )
            else:
                await self._send_message(
                    context,
                    text="Сообщение коммита пустое.",
                    **self._send_kwargs(chat_id, message_thread_id=message_thread_id),
                )
            return True
        session = self.manager.get(chat_id, session_id)
        if not session:
            await self._send_message(
                context,
                text="Сессия не найдена.",
                **self._send_kwargs(chat_id, message_thread_id=message_thread_id),
            )
            return True
        if not await self.ensure_git_repo(session, chat_id, context, message_thread_id=message_thread_id):
            return True
        if not await self.ensure_git_not_busy(session, chat_id, context, message_thread_id=message_thread_id):
            return True
        conflicts = await self._git_conflict_files(session)
        if conflicts:
            await self._handle_git_conflict(
                session,
                chat_id,
                context,
                message_thread_id=message_thread_id,
            )
            return True
        message = self._sanitize_commit_message(message)
        body = await self._build_commit_body(session)
        if body:
            body = self._sanitize_commit_body(body)
        await self._execute_git_commit(
            session,
            chat_id,
            context,
            message,
            body,
            message_thread_id=message_thread_id,
        )
        return True

    async def handle_callback(self, query, chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
        data = query.data or ""
        message_thread_id = self._query_thread_id(query)
        state_key = self._state_key(chat_id, message_thread_id)
        try:
            if data == "git_cancel":
                await self._edit_msg(context, query, "Операция отменена.")
                self.git_pending_ref.pop(state_key, None)
                self.git_branch_menu.pop(state_key, None)
                self.pending_git_commit.pop(state_key, None)
                return True
            if data == "git_pull_cancel":
                await self._edit_msg(context, query, "Pull отменен.")
                self.git_pull_target.pop(state_key, None)
                return True
            if data == "git_help":
                await self._edit_msg(context, query, "Готовлю git help…")
                session = await self.ensure_git_session(
                    chat_id,
                    context,
                    message_thread_id=message_thread_id,
                )
                if not session:
                    return True
                await self._send_git_help(
                    session,
                    chat_id,
                    context,
                    message_thread_id=message_thread_id,
                )
                return True
            if not (data.startswith("git_") or data.startswith("gitpull_") or data.startswith("git_conflict")):
                return False

            session = await self.ensure_git_session(
                chat_id,
                context,
                message_thread_id=message_thread_id,
            )
            if not session:
                return True
            if not await self.ensure_git_repo(session, chat_id, context, message_thread_id=message_thread_id):
                return True
            if data not in ("git_conflict_agent",) and not await self.ensure_git_not_busy(
                session,
                chat_id,
                context,
                message_thread_id=message_thread_id,
            ):
                return True

            if data == "git_status":
                await self._edit_msg(context, query, "Получаю git status…")
                text = await self._git_status_text(session)
                await self._send_git_message(
                    context,
                    chat_id,
                    session,
                    text,
                    message_thread_id=message_thread_id,
                )
                return True
            if data == "git_fetch":
                await self._edit_msg(context, query, "Выполняю git fetch…")
                if not await self._try_acquire_git_busy(session, chat_id, context, message_thread_id=message_thread_id):
                    return True
                try:
                    code, output = await self._run_git(session, ["fetch", "--prune"])
                    await self._send_git_output(
                        context,
                        chat_id,
                        session,
                        "Fetch",
                        output,
                        message_thread_id=message_thread_id,
                    )
                    if code == 0:
                        status = await self._git_status_text(session)
                        await self._send_git_message(
                            context,
                            chat_id,
                            session,
                            status,
                            message_thread_id=message_thread_id,
                        )
                finally:
                    session.git.busy = False
                return True
            if data == "git_pull":
                await self._edit_msg(context, query, "Проверяю возможность fast-forward…")
                if not await self._try_acquire_git_busy(session, chat_id, context, message_thread_id=message_thread_id):
                    return True
                try:
                    await self._run_git(session, ["fetch", "--prune"])
                    branch = await self._git_current_branch(session)
                    upstream = await self._git_upstream(session)
                    if not upstream and branch and branch != "HEAD":
                        candidate = f"origin/{branch}"
                        if await self._git_ref_exists(session, candidate):
                            upstream = candidate
                    if not upstream:
                        upstream = await self._git_default_remote(session)
                    if not upstream:
                        await self._send_git_message(
                            context,
                            chat_id,
                            session,
                            "Upstream не найден. Настройте upstream или выберите ветку через Merge/Rebase.",
                            message_thread_id=message_thread_id,
                        )
                        return True
                    ahead_behind = await self._git_ahead_behind(session, upstream)
                    if not ahead_behind:
                        await self._send_git_message(
                            context,
                            chat_id,
                            session,
                            "Не удалось определить ahead/behind. Проверьте состояние репозитория.",
                            message_thread_id=message_thread_id,
                        )
                        return True
                    ahead, behind = ahead_behind
                    if behind == 0 and ahead == 0:
                        await self._send_git_message(
                            context,
                            chat_id,
                            session,
                            "Ветка уже актуальна.",
                            message_thread_id=message_thread_id,
                        )
                        return True
                    if behind > 0 and ahead == 0:
                        code, output = await self._run_git(session, ["pull", "--ff-only"])
                        await self._send_git_output(
                            context,
                            chat_id,
                            session,
                            "Pull --ff-only",
                            output,
                            message_thread_id=message_thread_id,
                        )
                        if code == 0:
                            status = await self._git_status_text(session)
                            await self._send_git_message(
                                context,
                                chat_id,
                                session,
                                status,
                                message_thread_id=message_thread_id,
                            )
                        return True
                    self.git_pull_target[state_key] = upstream
                    prefix = self._session_label(session)
                    await self._send_message(
                        context,
                        text=f"{prefix}\nFast-forward невозможен. Ahead {ahead} / Behind {behind} относительно {upstream}.",
                        reply_markup=self._build_git_pull_keyboard(upstream),
                        **self._send_kwargs(chat_id, message_thread_id=message_thread_id),
                    )
                finally:
                    session.git.busy = False
                return True
            if data == "git_pull_merge":
                ref = self.git_pull_target.get(state_key)
                if not ref:
                    await self._edit_msg(context, query, "Цель pull не определена.")
                    return True
                await self._edit_msg(context, query, "Выполняю merge…")
                await self._git_merge_or_rebase(
                    session,
                    chat_id,
                    context,
                    "merge",
                    ref,
                    message_thread_id=message_thread_id,
                )
                self.git_pull_target.pop(state_key, None)
                return True
            if data == "git_pull_rebase":
                ref = self.git_pull_target.get(state_key)
                if not ref:
                    await self._edit_msg(context, query, "Цель pull не определена.")
                    return True
                await self._edit_msg(context, query, "Выполняю rebase…")
                await self._git_merge_or_rebase(
                    session,
                    chat_id,
                    context,
                    "rebase",
                    ref,
                    message_thread_id=message_thread_id,
                )
                self.git_pull_target.pop(state_key, None)
                return True
            if data == "git_merge_menu":
                await self._edit_msg(context, query, "Загружаю список веток…")
                code, output = await self._run_git(session, ["branch", "-r"])
                branches = [b.strip() for b in output.splitlines() if b.strip()] if code == 0 else []
                if not branches:
                    await self._edit_msg(context, query, "Нет удаленных веток.")
                    return True
                self.git_branch_menu[state_key] = branches
                prefix = self._session_label(session)
                await self._edit_msg(
                    context,
                    query,
                    f"{prefix}\nВыберите ветку для merge:",
                    reply_markup=self._build_git_branches_keyboard(
                        chat_id,
                        "merge",
                        message_thread_id=message_thread_id,
                    ),
                )
                return True
            if data == "git_rebase_menu":
                await self._edit_msg(context, query, "Загружаю список веток…")
                code, output = await self._run_git(session, ["branch", "-r"])
                branches = [b.strip() for b in output.splitlines() if b.strip()] if code == 0 else []
                if not branches:
                    await self._edit_msg(context, query, "Нет удаленных веток.")
                    return True
                self.git_branch_menu[state_key] = branches
                prefix = self._session_label(session)
                await self._edit_msg(
                    context,
                    query,
                    f"{prefix}\nВыберите ветку для rebase:",
                    reply_markup=self._build_git_branches_keyboard(
                        chat_id,
                        "rebase",
                        message_thread_id=message_thread_id,
                    ),
                )
                return True
            if data.startswith("git_merge_pick:") or data.startswith("git_rebase_pick:"):
                action = "merge" if data.startswith("git_merge_pick:") else "rebase"
                idx = int(data.split(":", 1)[1])
                branches = self.git_branch_menu.get(state_key, [])
                if idx < 0 or idx >= len(branches):
                    await self._edit_msg(context, query, "Выбор недоступен.")
                    return True
                ref = branches[idx]
                ahead_behind = await self._git_ahead_behind(session, ref)
                if not ahead_behind:
                    info = f"Не удалось определить ahead/behind относительно {ref}."
                else:
                    ahead, behind = ahead_behind
                    info = f"Ahead {ahead} / Behind {behind} относительно {ref}."
                self.git_pending_ref[state_key] = ref
                prefix = self._session_label(session)
                await self._edit_msg(
                    context,
                    query,
                    f"{prefix}\n{info}",
                    reply_markup=self._build_git_confirm_keyboard(action, ref),
                )
                return True
            if data == "git_confirm_merge" or data == "git_confirm_rebase":
                action = "merge" if data == "git_confirm_merge" else "rebase"
                ref = self.git_pending_ref.get(state_key)
                if not ref:
                    await self._edit_msg(context, query, "Ссылка не выбрана.")
                    return True
                await self._edit_msg(context, query, f"Выполняю {action}…")
                await self._git_merge_or_rebase(
                    session,
                    chat_id,
                    context,
                    action,
                    ref,
                    message_thread_id=message_thread_id,
                )
                self.git_pending_ref.pop(state_key, None)
                return True
            if data == "git_diff":
                await self._edit_msg(context, query, "Получаю diff…")
                code, output = await self._run_git(session, ["diff"])
                await self._send_git_output(
                    context,
                    chat_id,
                    session,
                    "Diff",
                    output,
                    message_thread_id=message_thread_id,
                )
                return True
            if data == "git_log":
                await self._edit_msg(context, query, "Получаю log…")
                code, output = await self._run_git(session, ["--no-pager", "log", "--oneline", "--decorate", "-n", "20"])
                await self._send_git_output(
                    context,
                    chat_id,
                    session,
                    "Log",
                    output,
                    message_thread_id=message_thread_id,
                )
                return True
            if data == "git_summary":
                await self._edit_msg(context, query, "Собираю git summary…")
                if not await self._try_acquire_git_busy(session, chat_id, context, message_thread_id=message_thread_id):
                    return True
                try:
                    code_status, status = await self._run_git(session, ["status", "--short", "--branch"])
                    code_stat, stat = await self._run_git(session, ["diff", "--stat"])
                    code_log, log = await self._run_git(session, ["--no-pager", "log", "--oneline", "--decorate", "-n", "10"])
                    text_parts = ["Git summary:"]
                    if code_status == 0 and status.strip():
                        text_parts.append("\nStatus:\n" + status.strip())
                    if code_stat == 0 and stat.strip():
                        text_parts.append("\nDiff --stat:\n" + stat.strip())
                    if code_log == 0 and log.strip():
                        text_parts.append("\nLog (last 10):\n" + log.strip())
                    await self._send_git_message(
                        context,
                        chat_id,
                        session,
                        "\n".join(text_parts)[:4000],
                        message_thread_id=message_thread_id,
                    )
                finally:
                    session.git.busy = False
                return True
            if data == "git_stash":
                await self._edit_msg(context, query, "Выполняю stash…")
                if not await self._try_acquire_git_busy(session, chat_id, context, message_thread_id=message_thread_id):
                    return True
                try:
                    code, output = await self._run_git(session, ["stash", "push", "-u"])
                    await self._send_git_output(
                        context,
                        chat_id,
                        session,
                        "Stash",
                        output,
                        message_thread_id=message_thread_id,
                    )
                    if code == 0:
                        status = await self._git_status_text(session)
                        await self._send_git_message(
                            context,
                            chat_id,
                            session,
                            status,
                            message_thread_id=message_thread_id,
                        )
                finally:
                    session.git.busy = False
                return True
            if data == "git_commit":
                try:
                    await self._edit_msg(context, query, "Готовлю commit…")
                except Exception:
                    pass
                conflicts = await self._git_conflict_files(session)
                if conflicts:
                    await self._handle_git_conflict(session, chat_id, context)
                    return True
                commit_context = await self._git_commit_context(session)
                if not commit_context:
                    await self._send_git_message(
                        context,
                        chat_id,
                        session,
                        "Не удалось получить diff для коммита.",
                        message_thread_id=message_thread_id,
                    )
                    return True
                commit_message = None
                commit_body = None
                if os.getenv("OPENAI_API_KEY") or self.config.defaults.openai_api_key:
                    from summary import suggest_commit_message_detailed_async
                    from utils.lang import resolve_user_lang
                    _lang = resolve_user_lang(self.config, chat_id=chat_id)
                    detailed = await suggest_commit_message_detailed_async(commit_context, self.config, language=_lang)
                    if detailed:
                        commit_message, commit_body = detailed
                if commit_message:
                    commit_message = self._sanitize_commit_message(commit_message)
                    if commit_body:
                        commit_body = self._sanitize_commit_body(commit_body)
                    else:
                        auto_body = await self._build_commit_body(session)
                        if auto_body:
                            commit_body = self._sanitize_commit_body(auto_body)
                    await self._execute_git_commit(
                        session,
                        chat_id,
                        context,
                        commit_message,
                        commit_body,
                        message_thread_id=message_thread_id,
                    )
                else:
                    self.pending_git_commit[state_key] = session.id
                    await self._send_git_message(
                        context,
                        chat_id,
                        session,
                        "Введите сообщение коммита (или '-' для отмены):",
                        message_thread_id=message_thread_id,
                    )
                return True
            if data == "git_push":
                await self._edit_msg(context, query, "Выполняю push…")
                if not await self._try_acquire_git_busy(session, chat_id, context, message_thread_id=message_thread_id):
                    return True
                try:
                    branch = await self._git_current_branch(session)
                    upstream = await self._git_upstream(session)
                    args = ["push"]
                    if branch and not upstream:
                        args += ["-u", "origin", branch]
                    code, output = await self._run_git(session, args)
                    await self._send_git_output(
                        context,
                        chat_id,
                        session,
                        "Push",
                        output,
                        message_thread_id=message_thread_id,
                    )
                    if code == 0:
                        status = await self._git_status_text(session)
                        await self._send_git_message(
                            context,
                            chat_id,
                            session,
                            status,
                            message_thread_id=message_thread_id,
                        )
                finally:
                    session.git.busy = False
                return True
            if data == "git_conflict_diff":
                await self._edit_msg(context, query, "Получаю diff…")
                code, output = await self._run_git(session, ["diff"])
                await self._send_git_output(
                    context,
                    chat_id,
                    session,
                    "Diff",
                    output,
                    message_thread_id=message_thread_id,
                )
                return True
            if data == "git_conflict_abort":
                mode = await self._git_in_progress(session)
                if not mode:
                    await self._edit_msg(context, query, "Нет активного merge/rebase.")
                    return True
                await self._edit_msg(context, query, "Выполняю abort…")
                cmd = ["merge", "--abort"] if mode == "merge" else ["rebase", "--abort"]
                code, output = await self._run_git(session, cmd)
                await self._send_git_output(
                    context,
                    chat_id,
                    session,
                    "Abort",
                    output,
                    message_thread_id=message_thread_id,
                )
                await self._git_conflict_files(session)
                if not session.git.conflict:
                    self._git_clear_conflict(session)
                return True
            if data == "git_conflict_continue":
                mode = await self._git_in_progress(session)
                if not mode:
                    await self._edit_msg(context, query, "Нет активного merge/rebase.")
                    return True
                await self._edit_msg(context, query, "Выполняю continue…")
                cmd = ["merge", "--continue"] if mode == "merge" else ["rebase", "--continue"]
                code, output = await self._run_git(session, cmd)
                await self._send_git_output(
                    context,
                    chat_id,
                    session,
                    "Continue",
                    output,
                    message_thread_id=message_thread_id,
                )
                conflicts = await self._git_conflict_files(session)
                if conflicts:
                    await self._handle_git_conflict(
                        session,
                        chat_id,
                        context,
                        message_thread_id=message_thread_id,
                    )
                return True
            if data == "git_conflict_agent":
                files = session.git.conflict_files or await self._git_conflict_files(session)
                files_text = ", ".join(files[:10]) if files else "нет файлов"
                note = (
                    "Нужна помощь с git-конфликтами. "
                    f"Список файлов: {files_text}. "
                    "Пожалуйста, предложи шаги для разрешения конфликтов и команды git."
                )
                await self._handle_cli_input(session, note, chat_id, context)
                if session.busy or session.is_active_by_tick():
                    await self._edit_msg(context, query, "Сессия занята. Выберите действие для очереди.")
                else:
                    await self._edit_msg(context, query, "Инструкция отправлена агенту.")
                return True
            return True
        except Exception as e:
            logging.exception(f"Ошибка git callback: {e}")
            await self._send_message(
                context,
                text=f"Ошибка выполнения git: {e}",
                **self._send_kwargs(chat_id, message_thread_id=message_thread_id),
            )
            return True
