from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional

from config import AppConfig
from .contracts import ExecutorRequest, PlanStep
from .dispatcher import Dispatcher
from .executor import Executor
from .planner import plan_steps


class OrchestratorRunner:
    def __init__(self, config: AppConfig):
        self._config = config
        self._executor = Executor(config)
        self._dispatcher = Dispatcher(config)

    async def run(self, session: Any, user_text: str, bot: Any, context: Any, dest: Dict[str, Any]) -> str:
        chat_id = dest.get("chat_id")
        chat_type = dest.get("chat_type")
        ctx_summary = f"session_id={session.id} chat_id={chat_id}"
        replan_count = 0
        while True:
            steps = plan_steps(self._config, user_text, ctx_summary)
            results: List[str] = []
            restart = False
            # Группируем по параллельности
            groups: Dict[Optional[str], List[PlanStep]] = {}
            for step in steps:
                groups.setdefault(step.parallel_group, []).append(step)

            # Сначала последовательные (parallel_group is None), затем остальные группы
            ordered_groups: List[List[PlanStep]] = []
            if None in groups:
                ordered_groups.append(groups.pop(None))
            ordered_groups.extend(groups.values())

            for group in ordered_groups:
                if len(group) == 1:
                    resp = await self._execute_step(group[0], session, bot, context, dest)
                    if group[0].step_type == "ask_user" and resp.status == "ok":
                        answer = ""
                        if resp.outputs:
                            answer = str(resp.outputs[0].get("content") or "")
                        if answer:
                            user_text = f"{user_text}\nОтвет пользователя: {answer}"
                            replan_count += 1
                            if replan_count > 2:
                                return "⚠️ Слишком много уточнений. Остановлено."
                            restart = True
                            break
                    results.append(resp.summary)
                else:
                    # параллельные независимые шаги
                    tasks = [self._execute_step(step, session, bot, context, dest) for step in group]
                    group_results = await asyncio.gather(*tasks)
                    results.extend([r.summary for r in group_results])
                if restart:
                    break

            if restart:
                continue

            return "\n\n".join([r for r in results if r]) or "(empty response)"

    async def _execute_step(self, step: PlanStep, session: Any, bot: Any, context: Any, dest: Dict[str, Any]):
        profile = self._dispatcher.get_profile(step)
        inputs = {}
        if step.step_type == "ask_user":
            inputs = {"question": step.ask_question, "options": step.ask_options}
        req = ExecutorRequest(
            task_id=step.id,
            goal=step.instruction,
            context="",
            allowed_tools=profile.allowed_tools,
            profile=profile.name,
            inputs=inputs,
        )
        resp = await self._executor.run(session, req, bot, context, dest, profile)
        if resp.status == "needs_input" and resp.next_questions:
            # Явный запрос пользователю: первая формулировка
            resp.summary = resp.next_questions[0]
        if resp.status == "needs_input" and not resp.next_questions:
            resp.summary = "Нужно уточнение пользователя, но вопрос не сформирован."
        return resp

    def record_message(self, chat_id: int, message_id: int) -> None:
        self._executor.record_message(chat_id, message_id)

    def resolve_question(self, question_id: str, answer: str) -> bool:
        return self._executor.resolve_question(question_id, answer)
