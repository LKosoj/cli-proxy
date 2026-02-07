from __future__ import annotations

import os
import time
from typing import Any, Callable, Dict, List

from telegram import Update
from telegram.ext import ContextTypes

from agent.plugins.base import DialogMixin, ToolPlugin
from agent.tooling.spec import ToolSpec
from agent.tooling.helpers import MEMORY_FILE


class MemoryTool(DialogMixin, ToolPlugin):
    # -- menu & commands ----------------------------------------------------

    def get_menu_label(self):
        return "Память"

    def get_menu_actions(self):
        return [
            {"label": "Показать", "action": "read"},
            {"label": "Добавить запись", "action": "append"},
            {"label": "Очистить", "action": "clear"},
        ]

    def get_commands(self) -> List[Dict[str, Any]]:
        return self._dialog_callback_commands()

    # -- DialogMixin contract -----------------------------------------------

    def dialog_steps(self):
        return {"wait_content": self._on_content}

    def callback_handlers(self) -> Dict[str, Callable]:
        return {
            "read": self._cb_read,
            "append": self._cb_append,
            "clear": self._cb_clear,
        }

    # -- spec ---------------------------------------------------------------

    def get_spec(self) -> ToolSpec:
        return ToolSpec(
            name="memory",
            description="Long-term memory. Use to save important info (project context, decisions, todos) or read previous notes. Memory persists across sessions.",
            parameters={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["read", "append", "clear"], "description": "read: get all memory, append: add new entry, clear: reset memory"},
                    "content": {"type": "string", "description": "For append: text to add (will be timestamped automatically)"},
                },
                "required": ["action"],
            },
            parallelizable=False,
        )

    # -- helpers ------------------------------------------------------------

    def _memory_path(self) -> str:
        if self.config:
            state_root = os.path.join(self.config.defaults.workdir, "_sandbox")
        else:
            state_root = os.getenv("AGENT_SANDBOX_ROOT") or os.getcwd()
        return os.path.join(state_root, MEMORY_FILE)

    # -- callback handlers --------------------------------------------------

    async def _cb_read(self, update: Update, context: ContextTypes.DEFAULT_TYPE, payload: str) -> None:
        query = update.callback_query
        if not query:
            return
        path = self._memory_path()
        if not os.path.exists(path):
            if query.message:
                await query.message.reply_text("(память пуста)")
            return
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read().strip() or "(память пуста)"
        except Exception:
            if query.message:
                await query.message.reply_text("Не удалось прочитать память.")
            return
        if query.message:
            await query.message.reply_text(content[:3500])

    async def _cb_append(self, update: Update, context: ContextTypes.DEFAULT_TYPE, payload: str) -> None:
        query = update.callback_query
        user_id = query.from_user.id if query and query.from_user else None
        chat_id = query.message.chat_id if query and query.message else None
        if not user_id or not chat_id:
            return
        self.start_dialog(chat_id, "wait_content", data={}, user_id=user_id)
        if query and query.message:
            await query.message.reply_text(
                "Отправьте текст для записи в память.\n\n"
                "Для отмены — кнопка ниже или текст: отмена, cancel, выход, -",
                reply_markup=self.cancel_markup(),
            )

    async def _cb_clear(self, update: Update, context: ContextTypes.DEFAULT_TYPE, payload: str) -> None:
        query = update.callback_query
        if not query:
            return
        path = self._memory_path()
        if os.path.exists(path):
            with open(path, "w", encoding="utf-8") as f:
                f.write("")
        if query.message:
            await query.message.reply_text("Память очищена.")

    # -- dialog step handler ------------------------------------------------

    async def _on_content(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        if not msg:
            return
        chat_id = update.effective_chat.id if update.effective_chat else 0
        text = (msg.text or "").strip()
        if not text:
            await msg.reply_text("Текст не может быть пустым. Попробуйте ещё раз.")
            return

        path = self._memory_path()
        timestamp = time.strftime("%Y-%m-%d %H:%M")
        entry = f"- {timestamp}: {text}\n"
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(entry)
        self.end_dialog(chat_id)
        await msg.reply_text("✅ Запись добавлена в память.")

    # -- execute (agent API) ------------------------------------------------

    async def execute(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        action = args.get("action")
        state_root = ctx.get("state_root") or ctx["cwd"]
        path = os.path.join(state_root, MEMORY_FILE)
        if action == "read":
            if not os.path.exists(path):
                return {"success": True, "output": "(memory is empty)"}
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            return {"success": True, "output": content or "(memory is empty)"}
        if action == "append":
            content = args.get("content")
            if not content:
                return {"success": False, "error": "Content required for append"}
            timestamp = time.strftime("%Y-%m-%d %H:%M")
            entry = f"- {timestamp}: {content.strip()}\n"
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(entry)
            return {"success": True, "output": "Memory updated"}
        if action == "clear":
            if os.path.exists(path):
                with open(path, "w", encoding="utf-8") as f:
                    f.write("")
            return {"success": True, "output": "Memory cleared"}
        return {"success": False, "error": f"Unknown action: {action}"}
