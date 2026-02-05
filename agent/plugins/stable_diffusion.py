from __future__ import annotations

import asyncio
import os
import tempfile
import logging
from typing import Any, Dict

import requests

from agent.plugins.base import ToolPlugin
from agent.tooling.spec import ToolSpec


class StableDiffusionTool(ToolPlugin):
    def get_source_name(self) -> str:
        return "HuggingFace Inference"

    def get_spec(self) -> ToolSpec:
        return ToolSpec(
            name="stable_diffusion",
            description="Генерация изображения по текстовому промпту через HuggingFace Inference API. Возвращает путь к PNG.",
            parameters={
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "Текстовый промпт"},
                    "model": {"type": "string", "description": "Модель HF (repo id)", "default": "HiDream-ai/HiDream-I1-Full"},
                },
                "required": ["prompt"],
            },
            parallelizable=True,
            timeout_ms=300_000,
        )

    async def execute(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        token = os.getenv("STABLE_DIFFUSION_TOKEN")
        if not token:
            return {"success": False, "error": "Не задан STABLE_DIFFUSION_TOKEN в окружении"}
        prompt = (args.get("prompt") or "").strip()
        if not prompt:
            return {"success": False, "error": "prompt обязателен"}
        model = (args.get("model") or "HiDream-ai/HiDream-I1-Full").strip()

        try:
            path = await asyncio.to_thread(self._generate_sync, token, model, prompt)
        except Exception as e:
            logging.exception(f"tool failed {str(e)}")
            return {"success": False, "error": f"Image generation failed: {e}"}

        return {"success": True, "output": path}

    def _generate_sync(self, token: str, model: str, prompt: str) -> str:
        url = f"https://api-inference.huggingface.co/models/{model}"
        headers = {"Authorization": f"Bearer {token}"}
        r = requests.post(url, headers=headers, json={"inputs": prompt}, timeout=120)
        if r.status_code == 503 and "estimated_time" in (r.text or ""):
            # Модель может быть в cold-start; пробуем еще раз.
            r = requests.post(url, headers=headers, json={"inputs": prompt}, timeout=180)
        if not r.ok:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")

        out_dir = os.path.join(tempfile.gettempdir(), "image_generation")
        os.makedirs(out_dir, exist_ok=True)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".png", dir=out_dir) as f:
            f.write(r.content)
            return f.name
