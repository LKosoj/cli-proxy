from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any, Dict, List, Optional

from config import AppConfig
from utils import sandbox_root
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

    def _load_session(self, cwd: str) -> Dict[str, Any]:
        path = os.path.join(cwd, "SESSION.json")
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    data.setdefault("orchestrator_by_task", {})
                    return data
            except Exception:
                return {"orchestrator_by_task": {}}
        return {"orchestrator_by_task": {}}

    def _save_session(self, cwd: str, session: Dict[str, Any]) -> None:
        path = os.path.join(cwd, "SESSION.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(session, f, ensure_ascii=False, indent=2)

    def _build_orchestrator_context(self, session_data: Dict[str, Any], task_key: str) -> str:
        history = session_data.get("orchestrator_by_task", {}).get(task_key, [])
        if not history:
            return ""
        recent = history[-25:]
        try:
            payload = json.dumps(recent, ensure_ascii=False)
        except Exception:
            return ""
        return f"\norchestrator_history:\n{payload}"

    async def run(self, session: Any, user_text: str, bot: Any, context: Any, dest: Dict[str, Any]) -> str:
        chat_id = dest.get("chat_id")
        chat_type = dest.get("chat_type")
        cwd = sandbox_root(self._config.defaults.workdir)
        os.makedirs(cwd, exist_ok=True)
        memory_text = read_memory(cwd)
        memory_context = trim_for_context(memory_text, max_chars=2000)
        ctx_summary = f"session_id={session.id} chat_id={chat_id}"
        if memory_context:
            ctx_summary = f"{ctx_summary}\nmemory:\n{memory_context}"
        task_key = session.id
        session_data = self._load_session(cwd)
        ctx_summary += self._build_orchestrator_context(session_data, task_key)
        replan_count = 0
        while True:
            steps = await plan_steps(self._config, user_text, ctx_summary)
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
            try:
                date_str = time.strftime("%Y-%m-%d")
                entry = {
                    "date": date_str,
                    "user": user_text,
                    "context": ctx_summary,
                    "steps": [step.model_dump() for step in steps],
                    "results": results,
                    "final": final_response,
                }
                session_data.setdefault("orchestrator_by_task", {}).setdefault(task_key, []).append(entry)
                # Не держим лишнее в памяти — только последние N записей.
                max_items = 50
                while len(session_data["orchestrator_by_task"][task_key]) > max_items:
                    session_data["orchestrator_by_task"][task_key].pop(0)
                self._save_session(cwd, session_data)
            except Exception:
                pass
            await self._maybe_update_memory(user_text, final_response, memory_text, cwd)
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

    def clear_session_cache(self, session_id: str) -> None:
        self._executor.clear_session_cache(session_id)

    async def _maybe_update_memory(self, user_text: str, final_response: str, memory_text: str, cwd: str) -> None:
        decision = await decide_memory_save(self._config, user_text, final_response, memory_text)
        if not decision:
            return
        tag, content = decision
        append_memory_tagged(cwd, tag, content)
        updated = read_memory(cwd)
        max_bytes = int(self._config.defaults.memory_max_kb) * 1024
        if memory_size_bytes(updated) <= max_bytes:
            return
        target_chars = int(self._config.defaults.memory_compact_target_kb) * 1024
        compacted = await compress_memory(self._config, updated, target_chars)
        if compacted:
            write_memory(cwd, compacted)
            return
        # Обязательная компрессия при лимите: если LLM недоступен, грубо ужимаем
        priority = ["PREF", "DECISION", "CONFIG", "AGREEMENT"]
        compacted_local = compact_memory_by_priority(updated, max_bytes, priority)
        write_memory(cwd, compacted_local)
