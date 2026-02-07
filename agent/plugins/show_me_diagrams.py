from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict

from agent.openai_client import create_async_openai_client
from agent.plugins.base import ToolPlugin
from agent.tooling.spec import ToolSpec


class ShowMeDiagramsTool(ToolPlugin):
    def __init__(self) -> None:
        super().__init__()
        self._jar_path = str(Path(__file__).with_name("plantuml.jar"))

    def get_source_name(self) -> str:
        return "PlantUML"

    def get_spec(self) -> ToolSpec:
        return ToolSpec(
            name="show_me_diagrams",
            description="Генерация диаграмм через PlantUML: генерирует PlantUML код и рендерит PNG. Возвращает путь к PNG и PlantUML код.",
            parameters={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["generate", "render"]},
                    "diagram_type": {
                        "type": "string",
                        "enum": ["gantt_chart", "mind_map", "flowchart", "project_timeline", "infographic", "org_chart", "process_diagram"],
                    },
                    "title": {"type": "string", "default": "Diagram"},
                    "description": {"type": "string", "description": "Для generate: описание диаграммы"},
                    "plantuml_code": {"type": "string", "description": "Для render: PlantUML код"},
                },
                "required": ["action"],
            },
            parallelizable=False,
            timeout_ms=120_000,
        )

    async def execute(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        action = args.get("action")
        if action not in ("generate", "render"):
            return {"success": False, "error": "action должен быть generate или render"}

        if not os.path.exists(self._jar_path):
            return {"success": False, "error": f"plantuml.jar не найден: {self._jar_path}"}

        title = (args.get("title") or "Diagram").strip()

        if action == "render":
            code = (args.get("plantuml_code") or "").strip()
            if not code:
                return {"success": False, "error": "plantuml_code обязателен для render"}
        else:
            diagram_type = (args.get("diagram_type") or "").strip()
            desc = (args.get("description") or "").strip()
            if not diagram_type or not desc:
                return {"success": False, "error": "Для generate нужны diagram_type и description"}
            code = await self._generate_plantuml(diagram_type, title, desc)

        try:
            png_path = await asyncio.to_thread(self._render_png_sync, code)
        except Exception as e:
            logging.exception(f"tool failed {str(e)}")
            return {"success": False, "error": f"PlantUML render failed: {e}"}

        out = f"file: {png_path}\n\n```plantuml\n{code.strip()}\n```"
        return {"success": True, "output": out}

    async def _generate_plantuml(self, diagram_type: str, title: str, description: str) -> str:
        cfg_def = getattr(getattr(self, "config", None), "defaults", None)
        api_key = os.getenv("OPENAI_API_KEY") or (cfg_def and getattr(cfg_def, "openai_api_key", None))
        base_url = os.getenv("OPENAI_BASE_URL") or (cfg_def and getattr(cfg_def, "openai_base_url", None))
        model = os.getenv("OPENAI_MODEL") or (cfg_def and getattr(cfg_def, "openai_model", None)) or "gpt-4o-mini"
        if not api_key:
            # Агент может сгенерировать PlantUML сам без этого инструмента, но здесь возвращаем явную ошибку.
            raise RuntimeError("Не задан OPENAI_API_KEY для генерации PlantUML кода")

        prompts = {
            "gantt_chart": "Сгенерируй PlantUML для диаграммы Ганта. Используй @startgantt/@endgantt.",
            "mind_map": "Сгенерируй PlantUML для mind map. Используй @startmindmap/@endmindmap.",
            "flowchart": (
                "Сгенерируй PlantUML для блок-схемы. "
                "Используй @startuml/@enduml, start/stop, :action; и if/else при необходимости."
            ),
            "project_timeline": "Сгенерируй PlantUML для таймлайна проекта в @startuml/@enduml.",
            "infographic": "Сгенерируй PlantUML для инфографики в @startuml/@enduml.",
            "org_chart": "Сгенерируй PlantUML для оргструктуры в @startuml/@enduml.",
            "process_diagram": "Сгенерируй PlantUML для диаграммы процесса в @startuml/@enduml.",
        }
        system = "Ты эксперт по PlantUML. Верни только валидный PlantUML код без пояснений и без ```."
        user = f"{prompts.get(diagram_type, '')}\nЗаголовок: {title}\nОписание: {description}\n"
        client = create_async_openai_client(api_key=api_key, base_url=(base_url or None))
        resp = await client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            temperature=0.2,
            max_tokens=1200,
        )
        code = (resp.choices[0].message.content or "").strip()
        return code

    def _render_png_sync(self, plantuml_code: str) -> str:
        tmp = tempfile.gettempdir()
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".puml", dir=tmp, encoding="utf-8") as f:
            puml_path = f.name
            f.write(plantuml_code)
        base = os.path.splitext(puml_path)[0]
        out_dir = tmp

        # java должен быть доступен в системе.
        result = subprocess.run(
            ["java", "-jar", self._jar_path, "-tpng", puml_path, "-o", out_dir],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError((result.stderr or result.stdout or "").strip()[:400])

        png_path = base + ".png"
        if not os.path.exists(png_path):
            # PlantUML может положить файл в out_dir с тем же именем.
            alt = os.path.join(out_dir, os.path.basename(png_path))
            if os.path.exists(alt):
                return alt
            raise RuntimeError("PNG файл не создан")
        return png_path
