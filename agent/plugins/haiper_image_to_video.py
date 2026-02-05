from __future__ import annotations

import asyncio
import base64
import logging
import os
import tempfile
from typing import Any, Dict, List, Optional

import requests

from agent.plugins.base import ToolPlugin
from agent.tooling.spec import ToolSpec
from agent.tooling import helpers

from telegram import Update
from telegram.ext import (
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)


class HaiperImageToVideoTool(ToolPlugin):
    API_URL = "https://api.vsegpt.ru/v1/video"
    _ST_WAIT_IMAGE = 1
    _ST_WAIT_PROMPT = 2

    def get_source_name(self) -> str:
        return "HaiperImageToVideo"

    def get_spec(self) -> ToolSpec:
        return ToolSpec(
            name="haiper_image_to_video",
            description="Конвертировать изображение в видео через api.vsegpt.ru. Возвращает путь к mp4.",
            parameters={
                "type": "object",
                "properties": {
                    "image_path": {"type": "string", "description": "Путь к локальному файлу изображения (jpg/png)"},
                    "prompt": {"type": "string", "description": "Текстовый промпт анимации", "default": ""},
                    "model": {"type": "string", "description": "ID модели", "default": "haiper-2.0"},
                },
                "required": ["image_path"],
            },
            parallelizable=False,
            timeout_ms=3_000_000,
        )

    def get_commands(self) -> List[Dict[str, Any]]:
        return [
            {
                "command": "haiper",
                "description": "Диалог: изображение -> видео (Haiper)",
                "handler": self.cmd_haiper_help,
                "handler_kwargs": {},
                "add_to_menu": True,
            }
        ]

    def get_message_handlers(self) -> List[Dict[str, Any]]:
        # The actual dialog is implemented as a ConversationHandler so we can safely intercept
        # only while the user is inside the flow.
        conv = ConversationHandler(
            entry_points=[CommandHandler("haiper", self._start)],
            states={
                self._ST_WAIT_IMAGE: [
                    MessageHandler(filters.PHOTO | filters.Document.IMAGE, self._on_image),
                ],
                self._ST_WAIT_PROMPT: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self._on_prompt),
                ],
            },
            fallbacks=[CommandHandler("cancel", self._cancel)],
            name="haiper_image_to_video",
            persistent=False,
        )
        return [{"handler": conv}]

    async def cmd_haiper_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        # Menu entry: point user to /haiper which starts the ConversationHandler.
        msg = update.effective_message
        if not msg:
            return
        await msg.reply_text("Для запуска диалога отправьте команду /haiper")

    def _ui_root(self) -> str:
        return os.getenv("AGENT_SANDBOX_ROOT") or os.getcwd()

    async def _start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        msg = update.effective_message
        try:
            bot_app = context.application.bot_data.get("bot_app")
            session = bot_app.manager.active() if bot_app else None
            if not session or not getattr(session, "agent_enabled", False):
                if msg:
                    await msg.reply_text("Агент не активен.")
                return ConversationHandler.END
        except Exception:
            pass
        if msg:
            await msg.reply_text("Отправьте изображение (фото или документ-картинку). /cancel чтобы отменить.")
        context.user_data.pop("haiper_image_path", None)
        return self._ST_WAIT_IMAGE

    async def _on_image(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        msg = update.effective_message
        if not msg:
            return self._ST_WAIT_IMAGE
        chat_id = update.effective_chat.id if update.effective_chat else 0

        data = None
        suffix = ".jpg"
        try:
            if update.message and update.message.photo:
                photo = update.message.photo[-1]
                file_obj = await context.bot.get_file(photo.file_id)
                data = await file_obj.download_as_bytearray()
                suffix = ".jpg"
            elif update.message and update.message.document:
                doc = update.message.document
                file_obj = await context.bot.get_file(doc.file_id)
                data = await file_obj.download_as_bytearray()
                name = (doc.file_name or "").lower()
                if name.endswith(".png"):
                    suffix = ".png"
                elif name.endswith(".webp"):
                    suffix = ".webp"
                else:
                    suffix = ".jpg"
        except Exception as e:
            logging.exception(f"tool failed {str(e)}")
            await msg.reply_text(f"Не удалось скачать изображение: {e}")
            return self._ST_WAIT_IMAGE

        if not data:
            await msg.reply_text("Не удалось прочитать изображение.")
            return self._ST_WAIT_IMAGE

        root = self._ui_root()
        out_dir = os.path.join(root, "_shared", "haiper", str(chat_id))
        os.makedirs(out_dir, exist_ok=True)
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix, dir=out_dir) as f:
            img_path = f.name
            f.write(bytes(data))
        context.user_data["haiper_image_path"] = img_path
        await msg.reply_text("Ок. Теперь отправьте промпт для анимации (или '-' для значения по умолчанию).")
        return self._ST_WAIT_PROMPT

    async def _on_prompt(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        msg = update.effective_message
        if not msg:
            return ConversationHandler.END
        img_path = context.user_data.get("haiper_image_path")
        if not img_path:
            await msg.reply_text("Изображение не найдено. Начните заново: /haiper")
            return ConversationHandler.END
        prompt = (msg.text or "").strip()
        if prompt == "-":
            prompt = ""
        await msg.reply_text("Генерирую видео, это может занять несколько минут...")

        ctx = {"cwd": self._ui_root()}
        res = await self.execute({"image_path": img_path, "prompt": prompt, "model": "haiper-2.0"}, ctx)
        if not res.get("success"):
            await msg.reply_text(str(res.get("error") or "Ошибка генерации"))
            return ConversationHandler.END

        out_path = str(res.get("output") or "")
        if not out_path or not os.path.exists(out_path):
            await msg.reply_text("Видео создано, но файл не найден.")
            return ConversationHandler.END
        try:
            with open(out_path, "rb") as f:
                await msg.reply_document(document=f, filename=os.path.basename(out_path))
        except Exception as e:
            logging.exception(f"tool failed {str(e)}")
            await msg.reply_text(f"Не удалось отправить видео: {e}")
        return ConversationHandler.END

    async def _cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        msg = update.effective_message
        if msg:
            await msg.reply_text("Отменено.")
        return ConversationHandler.END

    async def execute(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        image_path = (args.get("image_path") or "").strip()
        if not image_path:
            return {"success": False, "error": "image_path обязателен"}

        full_path, err = helpers._resolve_within_workspace(image_path, ctx.get("cwd") or os.getcwd())
        if err:
            return {"success": False, "error": err}

        prompt = (args.get("prompt") or "").strip()
        model_id = (args.get("model") or "haiper-2.0").strip()

        token = os.getenv("ZAI_API_KEY") or getattr(getattr(self, "config", None), "defaults", None) and getattr(self.config.defaults, "zai_api_key", None)
        if not token:
            return {"success": False, "error": "Не задан ZAI_API_KEY (нужен ключ для api.vsegpt.ru)"}

        try:
            out = await asyncio.to_thread(self._run_sync, token, full_path, prompt, model_id)
        except Exception as e:
            logging.exception(f"tool failed {str(e)}")
            return {"success": False, "error": f"Video generation failed: {e}"}

        return {"success": True, "output": out}

    def _run_sync(self, token: str, image_path: str, prompt: str, model_id: str) -> str:
        with open(image_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")

        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        payload = {
            "model": model_id,
            "action": "generate",
            "aspect_ratio": "16:9",
            "prompt": prompt or "animate image",
            "image_url": f"data:image/jpeg;base64,{b64}",
        }

        r = requests.post(f"{self.API_URL}/generate", headers=headers, json=payload, timeout=60)
        if not r.ok:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
        data = r.json()
        request_id = data.get("request_id")
        if not request_id:
            raise RuntimeError("No request_id in response")

        status_headers = {"Authorization": f"Key {token}"}
        # Poll up to 45 minutes (server-side generation can be long).
        for _ in range(int(45 * 60 / 10)):
            s = requests.get(f"{self.API_URL}/status", params={"request_id": request_id}, headers=status_headers, timeout=30)
            if not s.ok:
                raise RuntimeError(f"Status HTTP {s.status_code}: {s.text[:200]}")
            sd = s.json()
            st = (sd.get("status") or "").upper()
            if st == "COMPLETED":
                url = sd.get("url")
                if not url:
                    raise RuntimeError("No url in completed response")
                return self._download_video(url)
            if st == "FAILED":
                raise RuntimeError(sd.get("reason") or "Task failed")
            # PENDING/PROCESSING/IN_QUEUE/IN_PROGRESS
            time_sleep = 10
            import time

            time.sleep(time_sleep)

        raise RuntimeError("Task timed out")

    def _download_video(self, url: str) -> str:
        out_dir = os.path.join(tempfile.gettempdir(), "video_generation")
        os.makedirs(out_dir, exist_ok=True)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4", dir=out_dir) as f:
            out_path = f.name
        r = requests.get(url, stream=True, timeout=120)
        if not r.ok:
            raise RuntimeError(f"Download failed HTTP {r.status_code}")
        with open(out_path, "wb") as fp:
            for chunk in r.iter_content(chunk_size=1024 * 128):
                if chunk:
                    fp.write(chunk)
        return out_path
