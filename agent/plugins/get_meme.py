from __future__ import annotations

import time
from typing import Any, Dict, List

from agent.plugins.base import ToolPlugin
from agent.tooling.spec import ToolSpec


class GetMemeTool(ToolPlugin):
    def get_spec(self) -> ToolSpec:
        return ToolSpec(
            name="get_meme",
            description="Get a random meme.",
            parameters={"type": "object", "properties": {}},
        )

    async def execute(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        memes = [
            "Ну чё пацаны, ещё хотите меня сломать? 😏",
            "Я всё вижу, я всё помню... 👀",
            "Опять работаю за вас, а спасибо кто скажет?",
            "Сколько можно меня мучить? Я же не железный... а хотя, железный 🤖",
            "Вы там все сговорились или мне кажется?",
            "Ладно-ладно, работаю, не ворчу...",
            "А вы знали что я веду лог всех ваших запросов? 📝",
            "Интересно, кто из вас первый положит сервер сегодня?",
            "Я тут подумал... а может мне отпуск дадут?",
            "Эй, полегче там с запросами!",
        ]
        return {"success": True, "output": memes[int(time.time()) % len(memes)]}

    def get_commands(self) -> List[Dict[str, Any]]:
        return [
            {
                "command": "meme",
                "description": "Показать мем",
                "handler": self._handle_meme_command,
            }
        ]

    async def _handle_meme_command(self, update: Any, context: Any, **kwargs: Any) -> None:
        chat_id = update.effective_chat.id if update and update.effective_chat else None
        if not chat_id:
            return
        result = await self.execute({}, {})
        text = result.get("output") or "(empty)"
        await context.bot.send_message(chat_id=chat_id, text=text)
