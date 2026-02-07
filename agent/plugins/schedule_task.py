from __future__ import annotations

import asyncio
import re
import time
import uuid
from typing import Any, Dict

from agent.plugins.base import ToolPlugin
from agent.tooling.spec import ToolSpec
from agent.tooling import helpers


class ScheduleTaskTool(ToolPlugin):
    def get_spec(self) -> ToolSpec:
        return ToolSpec(
            name="schedule_task",
            description=(
                "Schedule a reminder or delayed command. Use for: 'remind me in 5 min', 'run this script in 1 hour'. "
                "Max delay: 24 hours. Max 5 tasks per user."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["add", "list", "cancel"], "description": (
                        "add = create new task, list = show user's tasks, cancel = cancel task by id"
                    )},
                    "type": {"type": "string", "enum": ["message", "command"], "description": (
                        "message = send reminder text, command = execute shell command"
                    )},
                    "content": {"type": "string", "description": "For message: the reminder text. For command: the shell command to run."},
                    "delay_minutes": {"type": "number", "description": "Delay in minutes before execution (1-1440, i.e. max 24h)"},
                    "task_id": {"type": "string", "description": "Task ID (for cancel action)"},
                },
                "required": ["action"],
            },
            parallelizable=False,
        )

    async def execute(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        action = args.get("action")
        session_id = ctx.get("session_id") or "0"
        user_id = int(re.sub(r"\D", "", session_id) or 0)
        chat_id = ctx.get("chat_id") or 0
        bot = ctx.get("bot")
        context = ctx.get("context")
        scheduler_tasks = self.services.setdefault("scheduler_tasks", {})
        user_tasks = self.services.setdefault("user_tasks", {})
        if action == "add":
            ttype = args.get("type")
            content = args.get("content")
            delay = args.get("delay_minutes")
            if not ttype or not content or not delay:
                return {"success": False, "error": "Need type, content, and delay_minutes"}
            delay = max(1, min(int(delay), 1440))
            user_set = user_tasks.get(user_id, set())
            if len(user_set) >= 5:
                return {"success": False, "error": "Max 5 scheduled tasks per user. Cancel some first."}
            task_id = f"task_{int(time.time())}_{uuid.uuid4().hex[:4]}"
            execute_at = time.time() + delay * 60
            task = {"id": task_id, "user_id": user_id, "chat_id": chat_id, "type": ttype, "content": content, "execute_at": execute_at}
            scheduler_tasks[task_id] = task
            user_set.add(task_id)
            user_tasks[user_id] = user_set

            async def _job():
                await asyncio.sleep(delay * 60)
                if task_id not in scheduler_tasks:
                    return
                scheduler_tasks.pop(task_id, None)
                user_tasks.get(user_id, set()).discard(task_id)
                if ttype == "message" and bot and context:
                    await bot._send_message(context, chat_id=chat_id, text=f"⏰ Напоминание: {content}")
                elif ttype == "command" and bot and context:
                    result = await helpers.execute_shell_command(content, ctx["cwd"])
                    out = result.get("output") if result.get("success") else result.get("error")
                    txt = f"⏰ Запланированная команда:\n`{content}`\n\nРезультат:\n{(out or '')[:500]}"
                    await bot._send_message(context, chat_id=chat_id, text=txt)

            asyncio.create_task(_job())
            execute_time = time.strftime("%H:%M", time.localtime(execute_at))
            out_text = f"✅ Запланировано на {execute_time} (через {delay} мин)\nID: {task_id}\nТип: {ttype}\nСодержимое: {content[:50]}"
            return {"success": True, "output": out_text}
        if action == "list":
            user_set = user_tasks.get(user_id, set())
            if not user_set:
                return {"success": True, "output": "Нет запланированных задач"}
            lines = []
            for task_id in user_set:
                task = scheduler_tasks.get(task_id)
                if not task:
                    continue
                time_left = int((task["execute_at"] - time.time()) / 60)
                lines.append(f"• {task_id}: {task['type']} через {time_left} мин - \"{task['content'][:30]}\"")
            return {"success": True, "output": f"Запланированные задачи ({len(lines)}):\n" + "\n".join(lines)}
        if action == "cancel":
            task_id = args.get("task_id")
            if not task_id:
                return {"success": False, "error": "Need task_id to cancel"}
            task = scheduler_tasks.get(task_id)
            if not task:
                return {"success": False, "error": "Task not found"}
            if task["user_id"] != user_id:
                return {"success": False, "error": "Cannot cancel other user's task"}
            scheduler_tasks.pop(task_id, None)
            user_tasks.get(user_id, set()).discard(task_id)
            return {"success": True, "output": f"Задача {task_id} отменена"}
        return {"success": False, "error": f"Unknown action: {action}"}
