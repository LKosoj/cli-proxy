from __future__ import annotations

import asyncio
import base64
import logging
import os
import tempfile
from typing import Any, Dict, List, Optional

import requests

from agent.plugins.base import DialogMixin, ToolPlugin
from agent.tooling.spec import ToolSpec
from agent.tooling import helpers

from telegram import Update
from telegram.ext import (
    ContextTypes,
    filters,
)


class HaiperImageToVideoTool(DialogMixin, ToolPlugin):
    API_URL = "https://api.vsegpt.ru/v1/video"
    # Video generation can take up to 45 minutes; extend dialog timeout.
    DIALOG_TIMEOUT = 60 * 60  # 1 hour

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

    def get_menu_label(self):
        return "Haiper (видео)"

    def get_menu_actions(self):
        return [{"label": "Создать видео", "action": "start"}]

    def get_commands(self) -> List[Dict[str, Any]]:
        return self._dialog_callback_commands()

    def callback_handlers(self):
        return {"start": self._cb_start}

    async def _cb_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE, payload: str) -> None:
        """Autonomous callback: start the image-to-video dialog."""
        query = update.callback_query
        chat_id = query.message.chat_id if query and query.message else 0
        self.start_dialog(chat_id, "wait_image")
        if query and query.message:
            await query.message.reply_text(
                "Отправьте изображение (фото или документ-картинку).\n"
                "Для выхода — кнопка ниже или текст: отмена, cancel, выход, -",
                reply_markup=self.cancel_markup(),
            )

    # -- DialogMixin contract -----------------------------------------------

    def dialog_steps(self):
        return {
            "wait_image": self._on_image_step,
            "wait_prompt": self._on_prompt_step,
        }

    def step_hint(self, step: str) -> Optional[str]:
        if step == "wait_image":
            return "Сейчас жду изображение. Отправьте картинку или напишите «отмена» / «cancel» / «-» для выхода."
        return None

    def extra_message_filters(self) -> Any:
        """Accept photos and image documents in addition to text."""
        return filters.PHOTO | filters.Document.IMAGE

    # get_message_handlers is provided by DialogMixin.

    # -- dialog entry -------------------------------------------------------

    def _ui_root(self) -> str:
        return os.getenv("AGENT_SANDBOX_ROOT") or os.getcwd()

    # -- step handlers ------------------------------------------------------

    async def _on_image_step(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Step wait_image: expect a photo/document-image."""
        msg = update.effective_message
        if not msg:
            return
        chat_id = update.effective_chat.id if update.effective_chat else 0

        # If this is a text message (not photo), show hint.
        has_photo = update.message and (update.message.photo or (
            update.message.document and update.message.document.mime_type
            and update.message.document.mime_type.startswith("image/")
        ))
        if not has_photo:
            hint = self.step_hint("wait_image")
            if hint:
                await msg.reply_text(hint)
            return

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
            return

        if not data:
            await msg.reply_text("Не удалось прочитать изображение.")
            return

        root = self._ui_root()
        out_dir = os.path.join(root, "_shared", "haiper", str(chat_id))
        os.makedirs(out_dir, exist_ok=True)
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix, dir=out_dir) as f:
            img_path = f.name
            f.write(bytes(data))

        self.set_step(chat_id, "wait_prompt", data={"image_path": img_path})
        await msg.reply_text(
            "Ок. Теперь отправьте промпт для анимации.\n"
            "Для выхода — кнопка ниже или текст: отмена, cancel, выход, -",
            reply_markup=self.cancel_markup(),
        )

    async def _on_prompt_step(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Step wait_prompt: receive text prompt and generate video."""
        msg = update.effective_message
        chat_id = update.effective_chat.id if update.effective_chat else 0
        if not msg:
            self.end_dialog(chat_id)
            return

        # Ignore non-text updates (e.g. accidental photo on the prompt step).
        if not msg.text:
            await msg.reply_text(
                "Сейчас жду текстовый промпт. Отправьте текст или напишите «отмена» / «cancel» / «-» для выхода."
            )
            return

        state = self.get_dialog(chat_id)
        img_path = (state.data.get("image_path") if state else None) or ""
        if not img_path:
            await msg.reply_text("Изображение не найдено. Начните заново: /haiper")
            self.end_dialog(chat_id)
            return

        prompt = (msg.text or "").strip()
        await msg.reply_text("Генерирую видео, это может занять несколько минут...")

        ctx = {"cwd": self._ui_root()}
        res = await self.execute({"image_path": img_path, "prompt": prompt, "model": "haiper-2.0"}, ctx)
        if not res.get("success"):
            await msg.reply_text(str(res.get("error") or "Ошибка генерации"))
            self.end_dialog(chat_id)
            return

        out_path = str(res.get("output") or "")
        if not out_path or not os.path.exists(out_path):
            await msg.reply_text("Видео создано, но файл не найден.")
            self.end_dialog(chat_id)
            return
        try:
            with open(out_path, "rb") as f:
                await msg.reply_document(document=f, filename=os.path.basename(out_path))
        except Exception as e:
            logging.exception(f"tool failed {str(e)}")
            await msg.reply_text(f"Не удалось отправить видео: {e}")
        self.end_dialog(chat_id)

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
