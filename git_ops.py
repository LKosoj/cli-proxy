import asyncio
import html
import os
import tempfile
import logging
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from session import Session, SessionManager
from utils import make_html_file


class GitOps:
    def __init__(
        self,
        config,
        manager: SessionManager,
        send_message,
        send_document,
        short_label,
        handle_cli_input,
    ) -> None:
        self.config = config
        self.manager = manager
        self._send_message = send_message
        self._send_document = send_document
        self._short_label = short_label
        self._handle_cli_input = handle_cli_input
        self.git_branch_menu: dict[int, list] = {}
        self.git_pending_ref: dict[int, str] = {}
        self.git_pull_target: dict[int, str] = {}
        self.pending_git_commit: dict[int, str] = {}
        self._git_askpass_path: Optional[str] = None

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

    def _build_git_branches_keyboard(self, chat_id: int, action: str) -> InlineKeyboardMarkup:
        branches = self.git_branch_menu.get(chat_id, [])
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
        if not hasattr(session, "git_busy"):
            session.git_busy = False
        if not hasattr(session, "git_conflict"):
            session.git_conflict = False
        if not hasattr(session, "git_conflict_files"):
            session.git_conflict_files = []
        if not hasattr(session, "git_conflict_kind"):
            session.git_conflict_kind = None

    async def ensure_git_session(self, chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> Optional[Session]:
        session = self.manager.active()
        if not session:
            await self._send_message(
                context,
                chat_id=chat_id,
                text="Нет активной сессии. Используйте /use для выбора.",
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
    ) -> None:
        prefix = self._session_label(session)
        await self._send_message(context, chat_id=chat_id, text=f"{prefix}\n{text}")

    async def ensure_git_repo(self, session: Session, chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
        code, output = await self._run_git(session, ["rev-parse", "--is-inside-work-tree"])
        if code != 0 or output.strip() != "true":
            await self._send_git_message(context, chat_id, session, "Каталог не является git-репозиторием.")
            return False
        return True

    async def ensure_git_not_busy(self, session: Session, chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
        self._ensure_git_state(session)
        if session.busy or session.is_active_by_tick():
            await self._send_git_message(
                context,
                chat_id,
                session,
                "CLI-сессия занята. Дождитесь завершения и попробуйте снова.",
            )
            return False
        if session.git_busy:
            await self._send_git_message(
                context,
                chat_id,
                session,
                "Git уже выполняется. Дождитесь завершения.",
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
        session.git_conflict = True
        session.git_conflict_files = files
        session.git_conflict_kind = kind

    def _git_clear_conflict(self, session: Session) -> None:
        session.git_conflict = False
        session.git_conflict_files = []
        session.git_conflict_kind = None

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

    async def _send_git_help(self, session: Session, chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
        path = os.path.join(os.path.dirname(__file__), "git.md")
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read().strip()
        except Exception as e:
            logging.exception(f"tool failed {str(e)}")
            await self._send_git_message(context, chat_id, session, f"Не удалось открыть git.md: {e}")
            return
        if not content:
            await self._send_git_message(context, chat_id, session, "git.md пустой.")
            return
        html_text = f"<pre>{html.escape(content)}</pre>"
        out_path = make_html_file(html_text, "git-help")
        try:
            await self._send_git_message(context, chat_id, session, "Git help:")
            with open(out_path, "rb") as f:
                await self._send_document(context, chat_id=chat_id, document=f)
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
    ) -> None:
        text = output.strip()
        if not text:
            await self._send_git_message(context, chat_id, session, f"{title}: готово.")
            return
        if len(text) > 4000:
            text = text[:4000]
        await self._send_git_message(context, chat_id, session, f"{title}:\n{text}")

    async def _execute_git_commit(
        self,
        session: Session,
        chat_id: int,
        context: ContextTypes.DEFAULT_TYPE,
        message: str,
        body: Optional[str] = None,
    ) -> None:
        session.git_busy = True
        try:
            code, add_out = await self._run_git(session, ["add", "-A"])
            if code != 0:
                await self._send_git_output(context, chat_id, session, "Git add", add_out)
                return
            args = ["commit", "-m", message]
            if body:
                args += ["-m", body]
            code, commit_out = await self._run_git(session, args)
            await self._send_git_output(context, chat_id, session, "Git commit", commit_out)
            if code == 0:
                status = await self._git_status_text(session)
                await self._send_git_message(context, chat_id, session, status)
        finally:
            session.git_busy = False

    async def _handle_git_conflict(self, session: Session, chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
        files = session.git_conflict_files or await self._git_conflict_files(session)
        files_text = ", ".join(files[:10]) if files else "нет файлов"
        text = f"Обнаружены git-конфликты: {files_text}"
        prefix = self._session_label(session)
        await self._send_message(
            context,
            chat_id=chat_id,
            text=f"{prefix}\n{text}",
            reply_markup=self._build_git_conflict_keyboard(),
        )

    async def _git_merge_or_rebase(self, session: Session, chat_id: int, context: ContextTypes.DEFAULT_TYPE, action: str, ref: str) -> None:
        session.git_busy = True
        try:
            code, output = await self._run_git(session, [action, ref])
            await self._send_git_output(context, chat_id, session, f"{action.title()} {ref}", output)
            conflicts = await self._git_conflict_files(session)
            if conflicts:
                await self._handle_git_conflict(session, chat_id, context)
            else:
                self._git_clear_conflict(session)
        finally:
            session.git_busy = False

    async def handle_pending_commit_message(self, chat_id: int, text: str, context: ContextTypes.DEFAULT_TYPE) -> bool:
        if chat_id not in self.pending_git_commit:
            return False
        session_id = self.pending_git_commit.pop(chat_id)
        message = text.strip()
        if message in ("-", "отмена", "Отмена"):
            session = self.manager.get(session_id)
            if session:
                await self._send_git_message(context, chat_id, session, "Коммит отменен.")
            else:
                await self._send_message(context, chat_id=chat_id, text="Коммит отменен.")
            return True
        if not message:
            session = self.manager.get(session_id)
            if session:
                await self._send_git_message(context, chat_id, session, "Сообщение коммита пустое.")
            else:
                await self._send_message(context, chat_id=chat_id, text="Сообщение коммита пустое.")
            return True
        session = self.manager.get(session_id)
        if not session:
            await self._send_message(context, chat_id=chat_id, text="Сессия не найдена.")
            return True
        if not await self.ensure_git_repo(session, chat_id, context):
            return True
        if not await self.ensure_git_not_busy(session, chat_id, context):
            return True
        conflicts = await self._git_conflict_files(session)
        if conflicts:
            await self._handle_git_conflict(session, chat_id, context)
            return True
        message = self._sanitize_commit_message(message)
        body = await self._build_commit_body(session)
        if body:
            body = self._sanitize_commit_body(body)
        await self._execute_git_commit(session, chat_id, context, message, body)
        return True

    async def handle_callback(self, query, chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
        data = query.data or ""
        try:
            if data == "git_cancel":
                await query.edit_message_text("Операция отменена.")
                self.git_pending_ref.pop(chat_id, None)
                self.git_branch_menu.pop(chat_id, None)
                self.pending_git_commit.pop(chat_id, None)
                return True
            if data == "git_pull_cancel":
                await query.edit_message_text("Pull отменен.")
                self.git_pull_target.pop(chat_id, None)
                return True
            if data == "git_help":
                await query.edit_message_text("Готовлю git help…")
                session = await self.ensure_git_session(chat_id, context)
                if not session:
                    return True
                await self._send_git_help(session, chat_id, context)
                return True
            if not (data.startswith("git_") or data.startswith("gitpull_") or data.startswith("git_conflict")):
                return False

            session = await self.ensure_git_session(chat_id, context)
            if not session:
                return True
            if not await self.ensure_git_repo(session, chat_id, context):
                return True
            if data not in ("git_conflict_agent",) and not await self.ensure_git_not_busy(session, chat_id, context):
                return True

            if data == "git_status":
                await query.edit_message_text("Получаю git status…")
                text = await self._git_status_text(session)
                await self._send_git_message(context, chat_id, session, text)
                return True
            if data == "git_fetch":
                await query.edit_message_text("Выполняю git fetch…")
                session.git_busy = True
                try:
                    code, output = await self._run_git(session, ["fetch", "--prune"])
                    await self._send_git_output(context, chat_id, session, "Fetch", output)
                    if code == 0:
                        status = await self._git_status_text(session)
                        await self._send_git_message(context, chat_id, session, status)
                finally:
                    session.git_busy = False
                return True
            if data == "git_pull":
                await query.edit_message_text("Проверяю возможность fast-forward…")
                session.git_busy = True
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
                        )
                        return True
                    ahead_behind = await self._git_ahead_behind(session, upstream)
                    if not ahead_behind:
                        await self._send_git_message(
                            context,
                            chat_id,
                            session,
                            "Не удалось определить ahead/behind. Проверьте состояние репозитория.",
                        )
                        return True
                    ahead, behind = ahead_behind
                    if behind == 0 and ahead == 0:
                        await self._send_git_message(context, chat_id, session, "Ветка уже актуальна.")
                        return True
                    if behind > 0 and ahead == 0:
                        code, output = await self._run_git(session, ["pull", "--ff-only"])
                        await self._send_git_output(context, chat_id, session, "Pull --ff-only", output)
                        if code == 0:
                            status = await self._git_status_text(session)
                            await self._send_git_message(context, chat_id, session, status)
                        return True
                    self.git_pull_target[chat_id] = upstream
                    prefix = self._session_label(session)
                    await self._send_message(
                        context,
                        chat_id=chat_id,
                        text=f"{prefix}\nFast-forward невозможен. Ahead {ahead} / Behind {behind} относительно {upstream}.",
                        reply_markup=self._build_git_pull_keyboard(upstream),
                    )
                finally:
                    session.git_busy = False
                return True
            if data == "git_pull_merge":
                ref = self.git_pull_target.get(chat_id)
                if not ref:
                    await query.edit_message_text("Цель pull не определена.")
                    return True
                await query.edit_message_text("Выполняю merge…")
                await self._git_merge_or_rebase(session, chat_id, context, "merge", ref)
                self.git_pull_target.pop(chat_id, None)
                return True
            if data == "git_pull_rebase":
                ref = self.git_pull_target.get(chat_id)
                if not ref:
                    await query.edit_message_text("Цель pull не определена.")
                    return True
                await query.edit_message_text("Выполняю rebase…")
                await self._git_merge_or_rebase(session, chat_id, context, "rebase", ref)
                self.git_pull_target.pop(chat_id, None)
                return True
            if data == "git_merge_menu":
                await query.edit_message_text("Загружаю список веток…")
                code, output = await self._run_git(session, ["branch", "-r"])
                branches = [b.strip() for b in output.splitlines() if b.strip()] if code == 0 else []
                if not branches:
                    await query.edit_message_text("Нет удаленных веток.")
                    return True
                self.git_branch_menu[chat_id] = branches
                prefix = self._session_label(session)
                await query.edit_message_text(
                    f"{prefix}\nВыберите ветку для merge:",
                    reply_markup=self._build_git_branches_keyboard(chat_id, "merge"),
                )
                return True
            if data == "git_rebase_menu":
                await query.edit_message_text("Загружаю список веток…")
                code, output = await self._run_git(session, ["branch", "-r"])
                branches = [b.strip() for b in output.splitlines() if b.strip()] if code == 0 else []
                if not branches:
                    await query.edit_message_text("Нет удаленных веток.")
                    return True
                self.git_branch_menu[chat_id] = branches
                prefix = self._session_label(session)
                await query.edit_message_text(
                    f"{prefix}\nВыберите ветку для rebase:",
                    reply_markup=self._build_git_branches_keyboard(chat_id, "rebase"),
                )
                return True
            if data.startswith("git_merge_pick:") or data.startswith("git_rebase_pick:"):
                action = "merge" if data.startswith("git_merge_pick:") else "rebase"
                idx = int(data.split(":", 1)[1])
                branches = self.git_branch_menu.get(chat_id, [])
                if idx < 0 or idx >= len(branches):
                    await query.edit_message_text("Выбор недоступен.")
                    return True
                ref = branches[idx]
                ahead_behind = await self._git_ahead_behind(session, ref)
                if not ahead_behind:
                    info = f"Не удалось определить ahead/behind относительно {ref}."
                else:
                    ahead, behind = ahead_behind
                    info = f"Ahead {ahead} / Behind {behind} относительно {ref}."
                self.git_pending_ref[chat_id] = ref
                prefix = self._session_label(session)
                await query.edit_message_text(
                    f"{prefix}\n{info}",
                    reply_markup=self._build_git_confirm_keyboard(action, ref),
                )
                return True
            if data == "git_confirm_merge" or data == "git_confirm_rebase":
                action = "merge" if data == "git_confirm_merge" else "rebase"
                ref = self.git_pending_ref.get(chat_id)
                if not ref:
                    await query.edit_message_text("Ссылка не выбрана.")
                    return True
                await query.edit_message_text(f"Выполняю {action}…")
                await self._git_merge_or_rebase(session, chat_id, context, action, ref)
                self.git_pending_ref.pop(chat_id, None)
                return True
            if data == "git_diff":
                await query.edit_message_text("Получаю diff…")
                code, output = await self._run_git(session, ["diff"])
                await self._send_git_output(context, chat_id, session, "Diff", output)
                return True
            if data == "git_log":
                await query.edit_message_text("Получаю log…")
                code, output = await self._run_git(session, ["--no-pager", "log", "--oneline", "--decorate", "-n", "20"])
                await self._send_git_output(context, chat_id, session, "Log", output)
                return True
            if data == "git_summary":
                await query.edit_message_text("Собираю git summary…")
                session.git_busy = True
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
                    await self._send_git_message(context, chat_id, session, "\n".join(text_parts)[:4000])
                finally:
                    session.git_busy = False
                return True
            if data == "git_stash":
                await query.edit_message_text("Выполняю stash…")
                session.git_busy = True
                try:
                    code, output = await self._run_git(session, ["stash", "push", "-u"])
                    await self._send_git_output(context, chat_id, session, "Stash", output)
                    if code == 0:
                        status = await self._git_status_text(session)
                        await self._send_git_message(context, chat_id, session, status)
                finally:
                    session.git_busy = False
                return True
            if data == "git_commit":
                try:
                    await query.edit_message_text("Готовлю commit…")
                except Exception:
                    pass
                conflicts = await self._git_conflict_files(session)
                if conflicts:
                    await self._handle_git_conflict(session, chat_id, context)
                    return True
                commit_context = await self._git_commit_context(session)
                if not commit_context:
                    await self._send_git_message(context, chat_id, session, "Не удалось получить diff для коммита.")
                    return True
                commit_message = None
                commit_body = None
                if os.getenv("OPENAI_API_KEY") or self.config.defaults.openai_api_key:
                    from summary import suggest_commit_message_detailed_async
                    detailed = await suggest_commit_message_detailed_async(commit_context, self.config)
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
                    await self._execute_git_commit(session, chat_id, context, commit_message, commit_body)
                else:
                    self.pending_git_commit[chat_id] = session.id
                    await self._send_git_message(context, chat_id, session, "Введите сообщение коммита (или '-' для отмены):")
                return True
            if data == "git_push":
                await query.edit_message_text("Выполняю push…")
                session.git_busy = True
                try:
                    branch = await self._git_current_branch(session)
                    upstream = await self._git_upstream(session)
                    args = ["push"]
                    if branch and not upstream:
                        args += ["-u", "origin", branch]
                    code, output = await self._run_git(session, args)
                    await self._send_git_output(context, chat_id, session, "Push", output)
                    if code == 0:
                        status = await self._git_status_text(session)
                        await self._send_git_message(context, chat_id, session, status)
                finally:
                    session.git_busy = False
                return True
            if data == "git_conflict_diff":
                await query.edit_message_text("Получаю diff…")
                code, output = await self._run_git(session, ["diff"])
                await self._send_git_output(context, chat_id, session, "Diff", output)
                return True
            if data == "git_conflict_abort":
                mode = await self._git_in_progress(session)
                if not mode:
                    await query.edit_message_text("Нет активного merge/rebase.")
                    return True
                await query.edit_message_text("Выполняю abort…")
                cmd = ["merge", "--abort"] if mode == "merge" else ["rebase", "--abort"]
                code, output = await self._run_git(session, cmd)
                await self._send_git_output(context, chat_id, session, "Abort", output)
                await self._git_conflict_files(session)
                if not session.git_conflict:
                    self._git_clear_conflict(session)
                return True
            if data == "git_conflict_continue":
                mode = await self._git_in_progress(session)
                if not mode:
                    await query.edit_message_text("Нет активного merge/rebase.")
                    return True
                await query.edit_message_text("Выполняю continue…")
                cmd = ["merge", "--continue"] if mode == "merge" else ["rebase", "--continue"]
                code, output = await self._run_git(session, cmd)
                await self._send_git_output(context, chat_id, session, "Continue", output)
                conflicts = await self._git_conflict_files(session)
                if conflicts:
                    await self._handle_git_conflict(session, chat_id, context)
                return True
            if data == "git_conflict_agent":
                files = session.git_conflict_files or await self._git_conflict_files(session)
                files_text = ", ".join(files[:10]) if files else "нет файлов"
                note = (
                    "Нужна помощь с git-конфликтами. "
                    f"Список файлов: {files_text}. "
                    "Пожалуйста, предложи шаги для разрешения конфликтов и команды git."
                )
                await self._handle_cli_input(session, note, chat_id, context)
                if session.busy or session.is_active_by_tick():
                    await query.edit_message_text("Сессия занята. Выберите действие для очереди.")
                else:
                    await query.edit_message_text("Инструкция отправлена агенту.")
                return True
            return True
        except Exception as e:
            logging.exception(f"Ошибка git callback: {e}")
            await self._send_message(context, chat_id=chat_id, text=f"Ошибка выполнения git: {e}")
            return True
