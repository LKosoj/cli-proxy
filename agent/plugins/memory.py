from __future__ import annotations

import os
from typing import Any, Callable, Dict

from telegram import Update
from telegram.ext import ContextTypes

from agent.plugins.base import DialogMixin, ToolPlugin
from modes.sdk.runtime.memory_store import (
    append_memory_structured,
    forget_memory_by_query,
    forget_memory_entry,
    read_memory,
    update_memory_entry,
)
from modes.sdk.runtime.tooling.spec import ToolSpec
from agent.tooling.helpers import MEMORY_FILE

_ALLOWED_TAGS = {"PREF", "DECISION", "CONFIG", "AGREEMENT"}
_CATEGORY_TO_TAG = {
    "preference": "PREF",
    "decision": "DECISION",
    "config": "CONFIG",
    "agreement": "AGREEMENT",
}


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
            description=(
                "Long-term memory. Use to save important info (project context, decisions, todos) or read previous notes. "
                "Memory persists across sessions."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["read", "append", "update", "forget", "clear"], "description": (
                        "read: get all memory, append: add new entry, update: edit by entry_id, "
                        "forget: delete by entry_id/query, clear: reset memory"
                    )},
                    "content": {"type": "string", "description": "For append: text to add (will be timestamped automatically)"},
                    "tag": {
                        "type": "string",
                        "enum": [
                            "PREF",
                            "DECISION",
                            "CONFIG",
                            "AGREEMENT",
                            "TASK",
                            "preference",
                            "decision",
                            "config",
                            "agreement",
                            "task_state",
                        ],
                        "description": "Optional for append. Memory category tag. Defaults to AGREEMENT.",
                    },
                    "layer": {"type": "string", "enum": ["semantic", "task_state"]},
                    "source": {
                        "type": "string",
                        "enum": ["agent", "system"],
                        "description": "Agent API source. User-sourced memory is reserved for trusted UI input.",
                    },
                    "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                    "ttl_days": {"type": "integer", "minimum": 1, "maximum": 365},
                    "verification_status": {"type": "string", "enum": ["verified", "unverified"]},
                    "evidence_type": {
                        "type": "string",
                        "enum": ["tool", "code", "config", "system", "none"],
                    },
                    "evidence_ref": {"type": "string", "description": "Short evidence reference for verified memory"},
                    "entry_id": {"type": "string", "description": "For update/forget by ID"},
                    "query": {"type": "string", "description": "For forget by text query"},
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

    def _normalize_tag(self, raw_tag: Any) -> str:
        tag = str(raw_tag or "").strip()
        if not tag:
            return "AGREEMENT"
        key = tag.lower()
        mapped = _CATEGORY_TO_TAG.get(key)
        if mapped:
            return mapped
        if key == "task_state":
            return "TASK"
        upper = tag.upper()
        if upper in _ALLOWED_TAGS or upper == "TASK":
            return upper
        return ""

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
        saved = append_memory_structured(
            os.path.dirname(path),
            tag="AGREEMENT",
            content=text,
            layer="semantic",
            source="user",
            confidence=0.7,
            ttl_days=None,
            verification_status="verified",
            evidence_type="user",
        )
        if not saved:
            await msg.reply_text("Не удалось добавить запись: нужен короткий атомарный факт (до 280 символов).")
            return
        self.end_dialog(chat_id)
        await msg.reply_text("✅ Запись добавлена в память.")

    # -- execute (agent API) ------------------------------------------------

    async def execute(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        action = args.get("action")
        state_root = ctx.get("state_root") or ctx["cwd"]
        path = os.path.join(state_root, MEMORY_FILE)
        if action == "read":
            content = read_memory(state_root)
            if not content:
                return {"success": True, "output": "(memory is empty)"}
            return {"success": True, "output": content or "(memory is empty)"}
        if action == "append":
            content = args.get("content")
            if not content:
                return {"success": False, "error": "Content required for append"}
            tag = self._normalize_tag(args.get("tag"))
            if not tag:
                return {"success": False, "error": "Invalid tag for append"}
            layer = str(args.get("layer") or "").strip().lower()
            if not layer:
                layer = "task_state" if tag == "TASK" else "semantic"
            if layer not in ("semantic", "task_state"):
                return {"success": False, "error": "Invalid layer for append"}
            source = str(args.get("source") or "agent").strip().lower()
            if source not in ("agent", "system"):
                return {"success": False, "error": "Invalid source for append"}
            confidence = args.get("confidence")
            ttl_days = args.get("ttl_days")
            verification_status = str(args.get("verification_status") or "").strip().lower()
            evidence_type = str(args.get("evidence_type") or "").strip().lower()
            evidence_ref = str(args.get("evidence_ref") or "").strip()
            if evidence_type == "user":
                return {"success": False, "error": "User evidence is reserved for trusted UI input"}
            if layer == "semantic":
                if verification_status != "verified" or evidence_type in ("", "none", "user"):
                    return {
                        "success": False,
                        "error": (
                            "Semantic memory append requires verification_status=verified "
                            "and non-user evidence_type other than none. "
                            "Use tag=TASK/layer=task_state for unverified notes."
                        ),
                    }
            else:
                verification_status = verification_status or "unverified"
                evidence_type = evidence_type or "none"
            saved = append_memory_structured(
                state_root,
                tag=tag,
                content=str(content),
                layer=layer,
                source=source,
                confidence=confidence,
                ttl_days=ttl_days,
                verification_status=verification_status,
                evidence_type=evidence_type,
                evidence_ref=evidence_ref,
            )
            if not saved:
                return {
                    "success": False,
                    "error": "Append rejected: content must be atomic (single short fact, <=280 chars)",
                }
            return {"success": True, "output": "Memory updated"}
        if action == "update":
            entry_id = str(args.get("entry_id") or "").strip()
            content = str(args.get("content") or "").strip()
            if not entry_id or not content:
                return {"success": False, "error": "entry_id and content required for update"}
            source = str(args.get("source") or "agent").strip().lower()
            if source not in ("agent", "system"):
                return {"success": False, "error": "Invalid source for update"}
            confidence = args.get("confidence")
            verification_status = args.get("verification_status")
            evidence_type = args.get("evidence_type")
            evidence_ref = args.get("evidence_ref")
            if str(evidence_type or "").strip().lower() == "user":
                return {"success": False, "error": "User evidence is reserved for trusted UI input"}
            ok = update_memory_entry(
                state_root,
                entry_id=entry_id,
                content=content,
                source=source,
                confidence=confidence,
                verification_status=verification_status,
                evidence_type=evidence_type,
                evidence_ref=evidence_ref,
            )
            if not ok:
                return {"success": False, "error": "Update failed: entry not found or invalid content"}
            return {"success": True, "output": "Memory updated"}
        if action == "forget":
            entry_id = str(args.get("entry_id") or "").strip()
            query = str(args.get("query") or "").strip()
            if entry_id:
                ok = forget_memory_entry(state_root, entry_id=entry_id)
                if not ok:
                    return {"success": False, "error": "Forget failed: entry not found"}
                return {"success": True, "output": "Memory entry removed"}
            if query:
                removed = forget_memory_by_query(state_root, query=query)
                return {"success": True, "output": f"Removed {removed} entrie(s)"}
            return {"success": False, "error": "entry_id or query required for forget"}
        if action == "clear":
            if os.path.exists(path):
                with open(path, "w", encoding="utf-8") as f:
                    f.write("")
            return {"success": True, "output": "Memory cleared"}
        return {"success": False, "error": f"Unknown action: {action}"}
