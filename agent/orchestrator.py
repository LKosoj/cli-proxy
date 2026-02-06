from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

from config import AppConfig
from utils import sandbox_root
from .contracts import ExecutorRequest, ExecutorResponse, PlanStep, validate_response
from .dispatcher import Dispatcher
from .executor import Executor
from .tooling.registry import get_tool_registry
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
        tool_registry = get_tool_registry(config)
        self._executor = Executor(config, tool_registry)
        self._dispatcher = Dispatcher(config, tool_registry)
        self._log = logging.getLogger(__name__)

    async def _sleep_backoff(self, attempt: int) -> None:
        # jittered exponential backoff
        await asyncio.sleep((0.6 * (2**attempt)) + (0.2 * (attempt % 3)))

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
            steps = self._order_steps_safely(steps)
            results: List[str] = []
            restart = False
            # Выполнение: по умолчанию строго последовательное.
            # Параллелим только то, что явно помечено parallelizable=true и не нарушает depends_on.
            for batch in self._iter_batches(steps):
                if len(batch) == 1:
                    step = batch[0]
                    resp = await self._execute_step(step, session, bot, context, dest, ctx_summary)
                    if step.step_type == "ask_user" and resp.status == "ok":
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
                    continue

                async def _run_one(s: PlanStep):
                    try:
                        return await self._execute_step(s, session, bot, context, dest, ctx_summary)
                    except Exception as e:
                        return type("Resp", (), {"status": "error", "summary": f"Ошибка шага {s.id}: {e}", "outputs": []})()

                group_results = await asyncio.gather(*[_run_one(s) for s in batch], return_exceptions=False)
                results.extend([r.summary for r in group_results if getattr(r, "summary", None)])

            if restart:
                continue

            final_response = "\n\n".join([r for r in results if r]) or "(empty response)"
            try:
                date_str = time.strftime("%Y-%m-%d")
                entry = {
                    "date": date_str,
                    "user": user_text,
                    "context": ctx_summary,
                    "steps": [dataclasses.asdict(step) for step in steps],
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

    def _order_steps_safely(self, steps: List[PlanStep]) -> List[PlanStep]:
        """
        Ensure plan has consistent ids and sane dependency references.
        We keep user-visible order stable; actual execution order is handled by _iter_batches().
        """
        ids = {s.id for s in steps if s.id}
        for s in steps:
            deps = []
            for d in (s.depends_on or []):
                if d and d in ids and d != s.id:
                    deps.append(d)
            s.depends_on = deps
        return steps

    def _iter_batches(self, steps: List[PlanStep]) -> List[List[PlanStep]]:
        """
        Yield execution batches. Each batch is either:
        - a single step (sequential execution), or
        - a set of parallelizable steps (executed via gather).
        """
        remaining = {s.id: s for s in steps}
        done: set[str] = set()
        order = [s.id for s in steps]

        batches: List[List[PlanStep]] = []
        while remaining:
            ready: List[PlanStep] = []
            for sid in order:
                s = remaining.get(sid)
                if not s:
                    continue
                if all((d in done) for d in (s.depends_on or [])):
                    ready.append(s)

            if not ready:
                # Cyclic or invalid deps. Fall back to sequential in original order.
                for sid in order:
                    s = remaining.get(sid)
                    if s:
                        batches.append([s])
                break

            # Partition ready steps into safe parallel groups.
            grouped: Dict[str, List[PlanStep]] = {}
            singles: List[PlanStep] = []
            for s in ready:
                gid = s.parallel_group
                if gid and s.parallelizable:
                    grouped.setdefault(gid, []).append(s)
                else:
                    singles.append(s)

            # Execute at most one parallel group at a time (conservative to avoid shared resource races).
            parallel_batch: Optional[List[PlanStep]] = None
            if grouped:
                # Pick the first group in stable order.
                for sid in order:
                    s = remaining.get(sid)
                    if not s:
                        continue
                    gid = s.parallel_group
                    if gid and gid in grouped:
                        parallel_batch = grouped[gid]
                        break

            if parallel_batch:
                batches.append(parallel_batch)
                for s in parallel_batch:
                    remaining.pop(s.id, None)
                    done.add(s.id)
                continue

            # Otherwise execute the first ready single step in stable order.
            next_single = None
            singles_set = {s.id for s in singles}
            for sid in order:
                if sid in singles_set:
                    next_single = remaining.get(sid)
                    break
            if not next_single:
                next_single = singles[0]
            batches.append([next_single])
            remaining.pop(next_single.id, None)
            done.add(next_single.id)

        return batches

    async def _execute_step(
        self, step: PlanStep, session: Any, bot: Any, context: Any, dest: Dict[str, Any], orchestrator_context: str
    ):
        profile = self._dispatcher.get_profile(step)
        inputs = {}
        if step.step_type == "ask_user":
            inputs = {"question": step.ask_question, "options": step.ask_options}
        corr_id = f"{session.id}:{step.id}"
        req = ExecutorRequest(
            task_id=step.id,
            goal=step.instruction,
            context=orchestrator_context or "",
            allowed_tools=profile.allowed_tools,
            profile=profile.name,
            inputs=inputs,
            corr_id=corr_id,
            # For now, constraints is only used as an extra system block for the agent.
            constraints=None,
        )
        timeout_ms = int(profile.timeout_ms)
        if req.deadline_ms:
            try:
                timeout_ms = min(timeout_ms, int(req.deadline_ms))
            except Exception:
                pass

        self._log.info("step start corr_id=%s step_type=%s", corr_id, step.step_type)
        resp: ExecutorResponse
        if step.step_type == "ask_user":
            resp = await self._executor.run(session, req, bot, context, dest, profile)
        else:
            last_timeout = False
            for attempt in range(max(0, int(profile.max_retries)) + 1):
                try:
                    resp = await asyncio.wait_for(
                        self._executor.run(session, req, bot, context, dest, profile),
                        timeout=timeout_ms / 1000.0,
                    )
                    last_timeout = False
                    break
                except asyncio.TimeoutError:
                    last_timeout = True
                    if attempt < int(profile.max_retries):
                        self._log.warning("step timeout, retrying corr_id=%s attempt=%s", corr_id, attempt)
                        await self._sleep_backoff(attempt)
                        continue
                    resp = ExecutorResponse(
                        task_id=req.task_id,
                        status="timeout",
                        summary=f"⏱️ Таймаут шага ({timeout_ms}ms).",
                        outputs=[],
                        tool_calls=[{"tool": "step", "error": "timeout", "corr_id": corr_id}],
                        next_questions=[],
                    )
                    validate_response(resp)
                    break
        self._log.info("step end corr_id=%s status=%s", corr_id, getattr(resp, "status", None))
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

    def get_plugin_commands(self, profile: Any) -> Dict[str, Any]:
        return self._executor.get_plugin_commands(profile)

    def get_plugin_ui(self, profile: Any) -> Dict[str, Any]:
        return self._executor.get_plugin_ui(profile)

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
