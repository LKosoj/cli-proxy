from __future__ import annotations

import hashlib
import logging
import os
from typing import Any, Callable, Dict, List

from openai import AsyncOpenAI

from agent.plugins.base import DialogMixin, ToolPlugin
from agent.tooling.spec import ToolSpec

from telegram import InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes


class TextDocumentQATool(DialogMixin, ToolPlugin):
    def get_source_name(self) -> str:
        return "TextDocumentQA"

    def get_spec(self) -> ToolSpec:
        return ToolSpec(
            name="text_document_qa",
            description="Работа с текстовыми документами: загрузить, список, вопрос по документу, удалить.",
            parameters={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["upload", "list", "ask", "delete"]},
                    "file_name": {"type": "string", "description": "Для upload: имя файла"},
                    "file_content": {"type": "string", "description": "Для upload: содержимое файла (текст)"},
                    "document_id": {"type": "string", "description": "Для ask/delete: ID документа"},
                    "query": {"type": "string", "description": "Для ask: вопрос к документу"},
                },
                "required": ["action"],
            },
            parallelizable=False,
            timeout_ms=120_000,
        )

    # -- menu & commands ----------------------------------------------------

    def get_menu_label(self):
        return "Документы"

    def get_menu_actions(self):
        return [
            {"label": "Список", "action": "list"},
            {"label": "Загрузить", "action": "upload"},
            {"label": "Задать вопрос", "action": "ask"},
        ]

    def get_commands(self) -> List[Dict[str, Any]]:
        return self._dialog_callback_commands()

    # -- DialogMixin contract -----------------------------------------------

    def dialog_steps(self):
        return {
            "wait_name": self._on_name,
            "wait_content": self._on_content,
            "wait_doc_select": {"callback": self._on_doc_select_button},
            "wait_query": self._on_query,
        }

    def callback_handlers(self) -> Dict[str, Callable]:
        return {
            "list": self._cb_list,
            "upload": self._cb_upload,
            "ask": self._cb_ask,
            "delete": self._cb_delete,
            "ask_doc": self._cb_ask_doc,
        }

    # -- helpers ------------------------------------------------------------

    def _storage_dir(self, state_root: str) -> str:
        return os.path.join(state_root, "text_document_qa")

    def _ui_state_root(self) -> str:
        return os.getenv("AGENT_SANDBOX_ROOT") or getattr(
            getattr(self, "config", None), "defaults", None
        ) and getattr(self.config.defaults, "workdir", None) or os.getcwd()

    def _list_documents(self) -> List[Dict[str, str]]:
        base = self._storage_dir(self._ui_state_root())
        os.makedirs(base, exist_ok=True)
        docs = []
        for fn in sorted(os.listdir(base)):
            if not fn.endswith(".txt"):
                continue
            doc_id = fn[:-4]
            meta = os.path.join(base, f"{doc_id}.meta")
            title = ""
            try:
                if os.path.exists(meta):
                    title = open(meta, "r", encoding="utf-8", errors="replace").read().strip()
            except Exception:
                title = ""
            docs.append({"id": doc_id, "title": title})
        return docs

    def _build_doc_list_keyboard(self, docs: List[Dict[str, str]]) -> List[list]:
        """Build inline keyboard showing documents with Delete and Ask buttons."""
        keyboard = []
        for doc in docs:
            did = doc["id"]
            title = (doc["title"] or did)[:40]
            keyboard.append([
                self.action_button(f"📄 {title}", "ask_doc", did),
                self.action_button("🗑", "delete", did),
            ])
        return keyboard

    # -- callback handlers --------------------------------------------------

    async def _cb_list(self, update: Update, context: ContextTypes.DEFAULT_TYPE, payload: str) -> None:
        """Show document list with action buttons."""
        query = update.callback_query
        if not query:
            return
        docs = self._list_documents()
        if not docs:
            if query.message:
                await query.message.reply_text("Документов нет.")
            return
        keyboard = self._build_doc_list_keyboard(docs)
        if query.message:
            await query.message.reply_text(
                "Документы (нажмите 📄 для вопроса, 🗑 для удаления):",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )

    async def _cb_upload(self, update: Update, context: ContextTypes.DEFAULT_TYPE, payload: str) -> None:
        """Start upload dialog."""
        query = update.callback_query
        user_id = query.from_user.id if query and query.from_user else None
        chat_id = query.message.chat_id if query and query.message else None
        if not user_id or not chat_id:
            return
        self.start_dialog(chat_id, "wait_name", data={"mode": "upload"}, user_id=user_id)
        if query and query.message:
            await query.message.reply_text(
                "Загрузка документа.\n"
                "Отправьте имя документа (одно слово или короткая фраза).\n\n"
                "Для отмены — кнопка ниже или текст: отмена, cancel, выход, -",
                reply_markup=self.cancel_markup(),
            )

    async def _cb_ask(self, update: Update, context: ContextTypes.DEFAULT_TYPE, payload: str) -> None:
        """Show document list for selecting which to ask a question about."""
        query = update.callback_query
        docs = self._list_documents()
        if not docs:
            if query and query.message:
                await query.message.reply_text("Документов нет. Сначала загрузите документ.")
            return
        user_id = query.from_user.id if query and query.from_user else None
        chat_id = query.message.chat_id if query and query.message else None
        if not user_id or not chat_id:
            return
        self.start_dialog(chat_id, "wait_doc_select", data={}, user_id=user_id)
        keyboard = []
        for doc in docs:
            did = doc["id"]
            title = (doc["title"] or did)[:40]
            keyboard.append([self.dialog_button(f"📄 {title}", did)])
        keyboard.append([self.dialog_button("Отмена", "_cancel")])
        if query and query.message:
            await query.message.reply_text(
                "Выберите документ для вопроса:",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )

    async def _cb_ask_doc(self, update: Update, context: ContextTypes.DEFAULT_TYPE, payload: str) -> None:
        """Direct 'ask question' on a specific doc from the list view."""
        query = update.callback_query
        doc_id = payload
        user_id = query.from_user.id if query and query.from_user else None
        chat_id = query.message.chat_id if query and query.message else None
        if not user_id or not chat_id:
            return
        self.start_dialog(chat_id, "wait_query", data={"doc_id": doc_id}, user_id=user_id)
        if query and query.message:
            await query.message.reply_text(
                f"Задайте вопрос по документу {doc_id}.\n\n"
                "Для отмены — кнопка ниже или текст: отмена, cancel, выход, -",
                reply_markup=self.cancel_markup(),
            )

    async def _cb_delete(self, update: Update, context: ContextTypes.DEFAULT_TYPE, payload: str) -> None:
        """Delete a document by its ID (payload)."""
        query = update.callback_query
        if not query:
            return
        doc_id = payload
        ctx = {"state_root": self._ui_state_root(), "cwd": os.getcwd()}
        res = await self.execute({"action": "delete", "document_id": doc_id}, ctx)
        if res.get("success"):
            await query.answer("Удалено")
        else:
            await query.answer(res.get("error", "Ошибка"), show_alert=True)
            return
        # Refresh the list.
        docs = self._list_documents()
        if not docs:
            await query.edit_message_text("Документов нет.")
            return
        keyboard = self._build_doc_list_keyboard(docs)
        await query.edit_message_text(
            "Документы (нажмите 📄 для вопроса, 🗑 для удаления):",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    # -- dialog step handlers -----------------------------------------------

    async def _on_name(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Step wait_name: user sends the document name."""
        msg = update.effective_message
        if not msg:
            return
        chat_id = update.effective_chat.id if update.effective_chat else 0
        name = (msg.text or "").strip()
        if not name:
            await msg.reply_text("Имя не может быть пустым. Попробуйте ещё раз.")
            return
        self.set_step(chat_id, "wait_content", data={"file_name": name})
        await msg.reply_text(
            f"Имя: {name}\n"
            "Теперь отправьте содержимое документа (текст).\n\n"
            "Для отмены — кнопка ниже или текст: отмена, cancel, выход, -",
            reply_markup=self.cancel_markup(),
        )

    async def _on_content(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Step wait_content: user sends the document content."""
        msg = update.effective_message
        if not msg:
            return
        chat_id = update.effective_chat.id if update.effective_chat else 0
        content = (msg.text or "").strip()
        if not content:
            await msg.reply_text("Содержимое не может быть пустым. Попробуйте ещё раз.")
            return

        state = self.get_dialog(chat_id)
        file_name = state.data.get("file_name", "untitled") if state else "untitled"

        ctx = {"state_root": self._ui_state_root(), "cwd": os.getcwd()}
        res = await self.execute({"action": "upload", "file_name": file_name, "file_content": content}, ctx)
        self.end_dialog(chat_id)
        if res.get("success"):
            await msg.reply_text(str(res.get("output") or "Загружено."))
        else:
            await msg.reply_text(str(res.get("error") or "Ошибка загрузки."))

    async def _on_doc_select_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Step wait_doc_select callback: user picks a doc button."""
        doc_id = self.parse_callback_payload(update)
        query = update.callback_query
        chat_id = query.message.chat_id if query and query.message else 0
        if not chat_id:
            return

        if doc_id == "_cancel":
            self.end_dialog(chat_id)
            if query and query.message:
                await query.message.reply_text("Отменено.")
            return

        self.set_step(chat_id, "wait_query", data={"doc_id": doc_id})
        if query and query.message:
            await query.message.reply_text(
                f"Задайте вопрос по документу {doc_id}.\n\n"
                "Для отмены — кнопка ниже или текст: отмена, cancel, выход, -",
                reply_markup=self.cancel_markup(),
            )

    async def _on_query(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Step wait_query: user sends the question text."""
        msg = update.effective_message
        if not msg:
            return
        chat_id = update.effective_chat.id if update.effective_chat else 0
        question = (msg.text or "").strip()
        if not question:
            await msg.reply_text("Вопрос не может быть пустым.")
            return

        state = self.get_dialog(chat_id)
        doc_id = state.data.get("doc_id", "") if state else ""
        if not doc_id:
            self.end_dialog(chat_id)
            await msg.reply_text("Документ не выбран. Попробуйте снова.")
            return

        await msg.reply_text("⏳ Ищу ответ...")
        ctx = {"state_root": self._ui_state_root(), "cwd": os.getcwd()}
        res = await self.execute({"action": "ask", "document_id": doc_id, "query": question}, ctx)
        self.end_dialog(chat_id)
        if res.get("success"):
            await msg.reply_text(str(res.get("output") or "Нет ответа."))
        else:
            await msg.reply_text(str(res.get("error") or "Ошибка."))

    # -- execute (agent API) ------------------------------------------------

    async def execute(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        action = args.get("action")
        state_root = ctx.get("state_root") or ctx.get("cwd") or os.getcwd()
        base = self._storage_dir(state_root)
        os.makedirs(base, exist_ok=True)

        if action == "upload":
            name = (args.get("file_name") or "").strip()
            content = args.get("file_content") or ""
            if not name or not content:
                return {"success": False, "error": "Для upload нужны file_name и file_content"}
            doc_id = hashlib.sha1((name + "\n" + content).encode("utf-8", errors="ignore")).hexdigest()[:12]
            path = os.path.join(base, f"{doc_id}.txt")
            meta = os.path.join(base, f"{doc_id}.meta")
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            with open(meta, "w", encoding="utf-8") as f:
                f.write(name)
            return {"success": True, "output": f"✅ Документ загружен\nID: {doc_id}\nИмя: {name}"}

        if action == "list":
            items = []
            for fn in sorted(os.listdir(base)):
                if not fn.endswith(".txt"):
                    continue
                doc_id = fn[:-4]
                meta = os.path.join(base, f"{doc_id}.meta")
                title = ""
                try:
                    if os.path.exists(meta):
                        title = open(meta, "r", encoding="utf-8", errors="replace").read().strip()
                except Exception:
                    title = ""
                items.append(f"• {doc_id}: {title}".strip())
            return {"success": True, "output": "Документы:\n" + ("\n".join(items) if items else "(нет)")}

        if action == "delete":
            doc_id = (args.get("document_id") or "").strip()
            if not doc_id:
                return {"success": False, "error": "Для delete нужен document_id"}
            path = os.path.join(base, f"{doc_id}.txt")
            meta = os.path.join(base, f"{doc_id}.meta")
            if not os.path.exists(path):
                return {"success": False, "error": "Документ не найден"}
            try:
                os.remove(path)
                if os.path.exists(meta):
                    os.remove(meta)
            except Exception as e:
                logging.exception(f"tool failed {str(e)}")
                return {"success": False, "error": f"Не удалось удалить: {e}"}
            return {"success": True, "output": f"🗑️ Удалено: {doc_id}"}

        if action == "ask":
            doc_id = (args.get("document_id") or "").strip()
            q = (args.get("query") or "").strip()
            if not doc_id or not q:
                return {"success": False, "error": "Для ask нужны document_id и query"}
            path = os.path.join(base, f"{doc_id}.txt")
            if not os.path.exists(path):
                return {"success": False, "error": "Документ не найден"}
            try:
                text = open(path, "r", encoding="utf-8", errors="replace").read()
            except Exception as e:
                return {"success": False, "error": f"Не удалось прочитать документ: {e}"}

            cfg_def = getattr(getattr(self, "config", None), "defaults", None)
            api_key = os.getenv("OPENAI_API_KEY") or (cfg_def and getattr(cfg_def, "openai_api_key", None))
            base_url = os.getenv("OPENAI_BASE_URL") or (cfg_def and getattr(cfg_def, "openai_base_url", None))
            model = os.getenv("OPENAI_MODEL") or (cfg_def and getattr(cfg_def, "openai_model", None)) or "gpt-4o-mini"
            if not api_key:
                return {"success": False, "error": "Не задан OPENAI_API_KEY"}

            context_text = text[:12000]
            client = AsyncOpenAI(api_key=api_key, base_url=(base_url or None))
            try:
                resp = await client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": "Отвечай строго по тексту документа. Если ответа нет, так и скажи."},
                        {"role": "user", "content": f"Документ:\n{context_text}\n\nВопрос:\n{q}\n\nОтвет:"},
                    ],
                    temperature=0.2,
                    max_tokens=800,
                )
                answer = (resp.choices[0].message.content or "").strip()
            except Exception as e:
                logging.exception(f"tool failed {str(e)}")
                return {"success": False, "error": f"LLM failed: {e}"}
            return {"success": True, "output": answer or "Нет ответа в документе."}

        return {"success": False, "error": f"Unknown action: {action}"}
