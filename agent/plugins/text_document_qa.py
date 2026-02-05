from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

from openai import AsyncOpenAI

from agent.plugins.base import ToolPlugin
from agent.tooling.spec import ToolSpec
from agent.tooling import helpers

from telegram import Update
from telegram.ext import ContextTypes


class TextDocumentQATool(ToolPlugin):
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

    def get_commands(self) -> List[Dict[str, Any]]:
        return [
            {
                "command": "list_documents",
                "description": "Показать список загруженных документов",
                "handler": self.cmd_list_documents,
                "handler_kwargs": {},
                "add_to_menu": True,
            },
            {
                "command": "upload_document",
                "description": "Загрузить документ. Формат: /upload_document <имя> <текст>",
                "args": "<имя> <текст>",
                "handler": self.cmd_upload_document,
                "handler_kwargs": {},
                "add_to_menu": False,
            },
            {
                "command": "ask_question",
                "description": "Вопрос по документу. Формат: /ask_question <doc_id> <вопрос>",
                "args": "<doc_id> <вопрос>",
                "handler": self.cmd_ask_question,
                "handler_kwargs": {},
                "add_to_menu": False,
            },
            {
                "command": "delete_document",
                "description": "Удалить документ. Формат: /delete_document <doc_id>",
                "args": "<doc_id>",
                "handler": self.cmd_delete_document,
                "handler_kwargs": {},
                "add_to_menu": False,
            },
        ]

    def _storage_dir(self, state_root: str) -> str:
        return os.path.join(state_root, "text_document_qa")

    def _ui_state_root(self) -> str:
        # Bot sets AGENT_SANDBOX_ROOT to a safe storage root.
        return os.getenv("AGENT_SANDBOX_ROOT") or getattr(getattr(self, "config", None), "defaults", None) and getattr(self.config.defaults, "workdir", None) or os.getcwd()

    async def cmd_list_documents(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        if not message:
            return
        ctx = {"state_root": self._ui_state_root(), "cwd": os.getcwd()}
        res = await self.execute({"action": "list"}, ctx)
        if res.get("success"):
            await message.reply_text(str(res.get("output") or ""))
        else:
            await message.reply_text(str(res.get("error") or "Ошибка"))

    async def cmd_upload_document(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        if not message:
            return
        text = (message.text or "").strip()
        parts = text.split(maxsplit=2)
        if len(parts) < 3:
            await message.reply_text("Использование: /upload_document <имя> <текст>")
            return
        _, name, content = parts
        ctx = {"state_root": self._ui_state_root(), "cwd": os.getcwd()}
        res = await self.execute({"action": "upload", "file_name": name, "file_content": content}, ctx)
        if res.get("success"):
            await message.reply_text(str(res.get("output") or ""))
        else:
            await message.reply_text(str(res.get("error") or "Ошибка"))

    async def cmd_ask_question(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        if not message:
            return
        text = (message.text or "").strip()
        parts = text.split(maxsplit=2)
        if len(parts) < 3:
            await message.reply_text("Использование: /ask_question <doc_id> <вопрос>")
            return
        _, doc_id, query = parts
        ctx = {"state_root": self._ui_state_root(), "cwd": os.getcwd()}
        res = await self.execute({"action": "ask", "document_id": doc_id, "query": query}, ctx)
        if res.get("success"):
            await message.reply_text(str(res.get("output") or ""))
        else:
            await message.reply_text(str(res.get("error") or "Ошибка"))

    async def cmd_delete_document(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        if not message:
            return
        args = context.args or []
        if not args:
            await message.reply_text("Использование: /delete_document <doc_id>")
            return
        doc_id = args[0]
        ctx = {"state_root": self._ui_state_root(), "cwd": os.getcwd()}
        res = await self.execute({"action": "delete", "document_id": doc_id}, ctx)
        if res.get("success"):
            await message.reply_text(str(res.get("output") or ""))
        else:
            await message.reply_text(str(res.get("error") or "Ошибка"))

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

            api_key = os.getenv("OPENAI_API_KEY") or getattr(getattr(self, "config", None), "defaults", None) and getattr(self.config.defaults, "openai_api_key", None)
            base_url = os.getenv("OPENAI_BASE_URL") or getattr(getattr(self, "config", None), "defaults", None) and getattr(self.config.defaults, "openai_base_url", None)
            model = os.getenv("OPENAI_MODEL") or getattr(getattr(self, "config", None), "defaults", None) and getattr(self.config.defaults, "openai_model", None) or "gpt-4o-mini"
            if not api_key:
                return {"success": False, "error": "Не задан OPENAI_API_KEY"}

            # Ограничиваем контекст: агенту не надо тащить весь документ.
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
