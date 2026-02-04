from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional

from config import AppConfig
from .contracts import ExecutorRequest, PlanStep
from .dispatcher import Dispatcher
from .executor import Executor
from .memory_policy import compress_memory, decide_memory_save
from .memory_store import (
    append_memory_tagged,
    compact_memory_by_priority,
    memory_size_bytes,
    read_memory,
    trim_for_context,
    write_memory,
)
from .planner import plan_steps


class OrchestratorRunner:
    def __init__(self, config: AppConfig):
        self._config = config
        self._executor = Executor(config)
        self._dispatcher = Dispatcher(config)

    async def run(self, session: Any, user_text: str, bot: Any, context: Any, dest: Dict[str, Any]) -> str:
        chat_id = dest.get("chat_id")
        chat_type = dest.get("chat_type")
        cwd = self._config.defaults.workdir
        memory_text = read_memory(cwd)
        memory_context = trim_for_context(memory_text, max_chars=2000)
        ctx_summary = f"session_id={session.id} chat_id={chat_id}"
        if memory_context:
            ctx_summary = f"{ctx_summary}\nmemory:\n{memory_context}"
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

            final_response = "\n\n".join([r for r in results if r]) or "(empty response)"
            self._maybe_update_memory(user_text, final_response, memory_text, cwd)
            return final_response

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

    def _maybe_update_memory(self, user_text: str, final_response: str, memory_text: str, cwd: str) -> None:
        decision = decide_memory_save(self._config, user_text, final_response, memory_text)
        if not decision:
            return
        tag, content = decision
        append_memory_tagged(cwd, tag, content)
        updated = read_memory(cwd)
        max_bytes = int(self._config.defaults.memory_max_kb) * 1024
        if memory_size_bytes(updated) <= max_bytes:
            return
        target_chars = int(self._config.defaults.memory_compact_target_kb) * 1024
        compacted = compress_memory(self._config, updated, target_chars)
        if compacted:
            write_memory(cwd, compacted)
            return
        # Обязательная компрессия при лимите: если LLM недоступен, грубо ужимаем
        priority = ["PREF", "DECISION", "CONFIG", "AGREEMENT"]
        compacted_local = compact_memory_by_priority(updated, max_bytes, priority)
        write_memory(cwd, compacted_local)
