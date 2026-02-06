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
from .session_store import read_json_locked, update_json_locked
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
from .openai_client import chat_completion


class OrchestratorRunner:
    def __init__(self, config: AppConfig):
        self._config = config
        tool_registry = get_tool_registry(config)
        self._executor = Executor(config, tool_registry)
        self._dispatcher = Dispatcher(config, tool_registry)
        self._log = logging.getLogger(__name__)

    def _load_session(self, cwd: str) -> Dict[str, Any]:
        path = os.path.join(cwd, "SESSION.json")
        data = read_json_locked(path, default={"orchestrator_by_task": {}})
        if isinstance(data, dict):
            data.setdefault("orchestrator_by_task", {})
            return data
        return {"orchestrator_by_task": {}}

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
        self._log.info("=== orchestrator run START session=%s chat=%s user_text=%r ===", session.id, chat_id, user_text[:200])
        memory_text = read_memory(cwd)
        memory_context = trim_for_context(memory_text, max_chars=2000)
        ctx_summary = f"session_id={session.id} chat_id={chat_id}"
        if memory_context:
            ctx_summary = f"{ctx_summary}\nmemory:\n{memory_context}"
            self._log.info("memory context loaded, %d chars", len(memory_context))
        task_key = session.id
        session_data = self._load_session(cwd)
        ctx_summary += self._build_orchestrator_context(session_data, task_key)
        replan_count = 0
        # Auto-replan after each executed step to adapt based on intermediate results.
        # We keep a small global map of prior step results by task_id so replans do not repeat work.
        prior_step_results: Dict[str, Dict[str, Any]] = {}
        appended_step_ids: set[str] = set()
        results: List[str] = []
        step_results: List[Dict[str, Any]] = []
        step_title_by_id: Dict[str, str] = {}

        def _compact_outputs(outputs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
            compacted: List[Dict[str, Any]] = []
            for o in outputs or []:
                if not isinstance(o, dict):
                    continue
                t = str(o.get("type") or "")
                if t == "text":
                    content = str(o.get("content") or "")
                    compacted.append(
                        {
                            "type": "text",
                            "content_len": len(content),
                            "content_preview": (content[:5000] + "...(truncated)") if len(content) > 5000 else content,
                        }
                    )
                    continue
                # Preserve non-text outputs as-is (e.g., file/image/audio references).
                compacted.append(dict(o))
            return compacted

        def _collect_artifacts_from_outputs(outputs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
            artifacts: List[Dict[str, Any]] = []
            for o in outputs or []:
                if not isinstance(o, dict):
                    continue
                t = str(o.get("type") or "")
                # Convention: {"type": "...", "path": "...", "name": "..."}.
                path = o.get("path") or o.get("file_path")
                if not path:
                    continue
                if t not in ("file", "document", "image", "audio", "video"):
                    # Keep it permissive: any output with a path is a candidate artifact.
                    t = "file"
                artifacts.append({"type": t, "path": str(path), "name": str(o.get("name") or "")})
            return artifacts

        async def _compose_final_answer_text(
            *,
            user_query: str,
            plan_steps_local: List[PlanStep],
            step_results_local: List[Dict[str, Any]],
        ) -> str:
            # Keep context bounded to avoid blowing up the model input.
            steps_payload: List[Dict[str, Any]] = []
            for s in plan_steps_local:
                steps_payload.append(
                    {
                        "id": s.id,
                        "title": s.title,
                        "step_type": s.step_type,
                        "depends_on": s.depends_on or [],
                    }
                )
            payload = {
                "user_query": user_query,
                "plan": steps_payload,
                "step_results": step_results_local[-25:],
            }
            try:
                raw = json.dumps(payload, ensure_ascii=False)
            except Exception:
                raw = ""
            if len(raw) > 40000:
                raw = raw[:40000] + "...(truncated)"
            system = (
                "Ты — оркестратор. Сформируй итоговый ответ пользователю по собранным материалам.\n"
                "Правила:\n"
                "- Не описывай внутренние шаги/инструменты/логи, только результат.\n"
                "- Если часть шагов завершилась ошибкой/blocked, кратко перечисли, что не удалось сделать.\n"
                "- Если есть допущения, явно отметь их.\n"
                "- Формат: Markdown, с короткими заголовками и списками.\n"
            )
            out = await chat_completion(self._config, system, f"Материалы (JSON):\n{raw}")
            return (out or "").strip()

        async def _send_final_answer(
            *,
            user_query: str,
            plan_steps_local: List[PlanStep],
            step_results_local: List[Dict[str, Any]],
        ) -> str:
            chat_id = dest.get("chat_id")
            if chat_id is None or bot is None:
                # No transport: fall back to returning a textual response to the caller.
                return "\n\n".join([r.get("summary") for r in step_results_local if r.get("summary")]) or "(empty response)"

            corr_id = f"{session.id}:compose_final_answer"
            self._log.info("step start corr_id=%s step_type=%s", corr_id, "compose_final_answer")
            try:
                final_text = await _compose_final_answer_text(
                    user_query=user_query,
                    plan_steps_local=plan_steps_local,
                    step_results_local=step_results_local,
                )
                if not final_text:
                    final_text = "\n\n".join([r.get("summary") for r in step_results_local if r.get("summary")]) or "(empty response)"

                # Collect artifacts before sending the main answer (but send them after the main HTML+summary).
                artifacts: List[Dict[str, Any]] = []
                for r in step_results_local:
                    artifacts.extend(_collect_artifacts_from_outputs(r.get("outputs") or []))

                # 1) Short ready message
                try:
                    await bot._send_message(context, chat_id=chat_id, text="Готово. Результат ниже.")
                except Exception as e:
                    self._log.exception("compose_final_answer: failed to send ready message: %s", e)

                async def _send_payload_bg() -> None:
                    # 2) One HTML+summary via send_output (no header)
                    try:
                        await bot.send_output(
                            session,
                            dest,
                            final_text,
                            context,
                            send_header=False,
                            force_html=True,
                        )
                    except Exception as e:
                        self._log.exception("compose_final_answer: failed to send final output: %s", e)

                    # 3) Additional materials as separate messages
                    for a in artifacts:
                        path = a.get("path") or ""
                        if not path or not os.path.exists(path):
                            continue
                        try:
                            with open(path, "rb") as f:
                                await bot._send_document(context, chat_id=chat_id, document=f)
                        except Exception as e:
                            self._log.exception("compose_final_answer: failed to send artifact %r: %s", path, e)

                # Do not block the update handler/polling loop on heavy HTML generation/upload.
                task = asyncio.create_task(_send_payload_bg())

                def _cb(t: asyncio.Task) -> None:
                    try:
                        t.result()
                    except asyncio.CancelledError:
                        return
                    except Exception as e:
                        self._log.exception("compose_final_answer: background send failed: %s", e)

                task.add_done_callback(_cb)

                self._log.info("step end corr_id=%s status=%s", corr_id, "ok")
                return final_text
            except Exception as e:
                self._log.exception("step end corr_id=%s status=error err=%s", corr_id, e)
                return "\n\n".join([r.get("summary") for r in step_results_local if r.get("summary")]) or "(empty response)"

        def _seed_completed_sets(steps: List[PlanStep]) -> tuple[set[str], set[str]]:
            ok: set[str] = set()
            fail: set[str] = set()
            for s in steps:
                prev = prior_step_results.get(s.id)
                if not prev:
                    continue
                st = str(prev.get("status") or "")
                if self._is_success_status(st):
                    ok.add(s.id)
                else:
                    fail.add(s.id)
            return ok, fail

        def _progress_context() -> str:
            # Keep the context bounded: include only the last N step summaries.
            tail = step_results[-25:]
            try:
                payload = json.dumps(tail, ensure_ascii=False)
            except Exception:
                payload = ""
            if not payload:
                return ""
            return f"\nstep_results_so_far:\n{payload}"

        while True:
            self._log.info("--- planning (attempt %d) ---", replan_count + 1)
            steps = await plan_steps(self._config, user_text, ctx_summary + _progress_context())
            steps = self._order_steps_safely(steps)
            step_title_by_id = {s.id: s.title for s in steps if s.id}
            self._log.info("plan ready: %d step(s) -> %s",
                           len(steps),
                           ", ".join(f"{s.id}({s.step_type})" for s in steps))
            restart = False
            # Dynamic graph execution:
            # - respects depends_on
            # - only executes dependents if their deps succeeded (ok/partial)
            # - parallelizes only explicitly-marked steps
            completed_ok, completed_fail = _seed_completed_sets(steps)

            while True:
                batch, skipped = self._next_batch(steps, completed_ok, completed_fail, session_id=session.id)
                if skipped:
                    self._log.info("skipped %d step(s): %s", len(skipped),
                                   ", ".join(f"{r.task_id}({r.status})" for r in skipped))
                for r in skipped:
                    # Record skipped once (across replans).
                    if r.task_id not in appended_step_ids:
                        appended_step_ids.add(r.task_id)
                        results.append(r.summary)
                        entry = {
                            "task_id": r.task_id,
                            "status": r.status,
                            "summary": r.summary,
                            "title": step_title_by_id.get(r.task_id),
                            "outputs": _compact_outputs(r.outputs or []),
                            "tool_calls": r.tool_calls,
                        }
                        step_results.append(entry)
                        prior_step_results[r.task_id] = entry
                if not batch:
                    self._log.info("no more steps to execute, finishing")
                    break

                self._log.info("executing batch: %s (parallel=%s)",
                               ", ".join(s.id for s in batch), len(batch) > 1)
                if len(batch) == 1:
                    step = batch[0]
                    resp = await self._execute_step(step, session, bot, context, dest, ctx_summary)
                    self._apply_step_result(step, resp, completed_ok, completed_fail)
                    if step.step_type == "ask_user" and resp.status == "ok":
                        answer = ""
                        if resp.outputs:
                            answer = str(resp.outputs[0].get("content") or "")
                        if answer:
                            self._log.info("ask_user answer received, will replan: %r", answer[:200])
                            user_text = f"{user_text}\nОтвет пользователя: {answer}"
                            replan_count += 1
                            if replan_count > 2:
                                self._log.warning("too many clarifications (%d), stopping", replan_count)
                                return "⚠️ Слишком много уточнений. Остановлено."
                            restart = True
                            break
                    # Record result once (across replans).
                    if resp.task_id not in appended_step_ids:
                        appended_step_ids.add(resp.task_id)
                        results.append(resp.summary)
                        entry = {
                            "task_id": resp.task_id,
                            "status": resp.status,
                            "summary": resp.summary,
                            "title": step_title_by_id.get(resp.task_id),
                            "outputs": _compact_outputs(resp.outputs or []),
                            "tool_calls": resp.tool_calls,
                        }
                        step_results.append(entry)
                        prior_step_results[resp.task_id] = entry

                    # Auto-replan after each executed step if there is remaining work in this plan.
                    remaining = [s for s in steps if s.id not in completed_ok and s.id not in completed_fail]
                    if remaining:
                        replan_count += 1
                        if replan_count > 25:
                            self._log.warning("too many replans (%d), stopping replanning", replan_count)
                        else:
                            restart = True
                            break
                    continue

                async def _run_one(s: PlanStep):
                    try:
                        return await self._execute_step(s, session, bot, context, dest, ctx_summary)
                    except Exception as e:
                        return ExecutorResponse(
                            task_id=s.id,
                            status="error",
                            summary=f"Ошибка шага {s.id}: {e}",
                            outputs=[],
                            tool_calls=[{"tool": "step", "error": str(e), "corr_id": f"{session.id}:{s.id}"}],
                            next_questions=[],
                        )

                group_results = await asyncio.gather(*[_run_one(s) for s in batch], return_exceptions=False)
                for s, r in zip(batch, group_results):
                    self._apply_step_result(s, r, completed_ok, completed_fail)
                    self._log.info("parallel step %s finished: status=%s", s.id, getattr(r, "status", "?"))
                for r in group_results:
                    if r.task_id in appended_step_ids:
                        continue
                    appended_step_ids.add(r.task_id)
                    if getattr(r, "summary", None):
                        results.append(r.summary)
                    entry = {
                        "task_id": r.task_id,
                        "status": r.status,
                        "summary": r.summary,
                        "title": step_title_by_id.get(r.task_id),
                        "outputs": _compact_outputs(r.outputs or []),
                        "tool_calls": r.tool_calls,
                    }
                    step_results.append(entry)
                    prior_step_results[r.task_id] = entry

                # Auto-replan after each batch if there is remaining work in this plan.
                remaining = [s for s in steps if s.id not in completed_ok and s.id not in completed_fail]
                if remaining:
                    replan_count += 1
                    if replan_count > 25:
                        self._log.warning("too many replans (%d), stopping replanning", replan_count)
                    else:
                        restart = True
                        break

            if restart:
                continue

            final_response = await _send_final_answer(
                user_query=user_text,
                plan_steps_local=steps,
                step_results_local=step_results,
            )
            self._log.info("=== orchestrator run END session=%s ok=%d fail=%d response_len=%d ===",
                           session.id, len(completed_ok), len(completed_fail), len(final_response))
            try:
                date_str = time.strftime("%Y-%m-%d")
                entry = {
                    "date": date_str,
                    "user": user_text,
                    "context": ctx_summary,
                    "steps": [dataclasses.asdict(step) for step in steps],
                    "results": results,
                    "step_results": step_results,
                    "final": final_response,
                }
                path = os.path.join(cwd, "SESSION.json")

                def _append(current: Dict[str, Any]) -> Dict[str, Any]:
                    current.setdefault("orchestrator_by_task", {})
                    current["orchestrator_by_task"].setdefault(task_key, []).append(entry)
                    # Не держим лишнее в памяти — только последние N записей.
                    max_items = 50
                    items = current["orchestrator_by_task"][task_key]
                    while len(items) > max_items:
                        items.pop(0)
                    return current

                update_json_locked(path, _append, default={"orchestrator_by_task": {}})
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

    def _is_success_status(self, status: str) -> bool:
        return status in ("ok", "partial")

    def _apply_step_result(
        self,
        step: PlanStep,
        resp: ExecutorResponse,
        completed_ok: set[str],
        completed_fail: set[str],
    ) -> None:
        if self._is_success_status(getattr(resp, "status", "error")):
            completed_ok.add(step.id)
            completed_fail.discard(step.id)
        else:
            completed_fail.add(step.id)
            completed_ok.discard(step.id)

    def _next_batch(
        self,
        steps: List[PlanStep],
        completed_ok: set[str],
        completed_fail: set[str],
        session_id: str,
    ) -> tuple[List[PlanStep], List[ExecutorResponse]]:
        """
        Pick next executable batch based on dependency success.
        Returns [] when no runnable steps remain.
        """
        remaining = [s for s in steps if s.id not in completed_ok and s.id not in completed_fail]
        if not remaining:
            return [], []

        ids = {s.id for s in steps}
        # Compute ready steps: deps must be completed successfully.
        ready: List[PlanStep] = []
        blocked: List[PlanStep] = []
        for s in remaining:
            deps = [d for d in (s.depends_on or []) if d in ids and d != s.id]
            if any(d in completed_fail for d in deps):
                blocked.append(s)
                continue
            if all(d in completed_ok for d in deps):
                ready.append(s)

        # Mark blocked steps as failed (dependency failed) to avoid infinite loop.
        skipped_responses: List[ExecutorResponse] = []
        for s in blocked:
            completed_fail.add(s.id)
            failed_deps = [d for d in (s.depends_on or []) if d in completed_fail]
            corr_id = f"{session_id}:{s.id}"
            resp = ExecutorResponse(
                task_id=s.id,
                status="blocked",
                summary=f"⛔ Шаг {s.id} пропущен: зависимость не выполнена ({', '.join(failed_deps) or 'unknown'}).",
                outputs=[],
                tool_calls=[{"tool": "orchestrator", "error": "dependency_failed", "corr_id": corr_id, "depends_on": failed_deps}],
                next_questions=[],
            )
            validate_response(resp)
            skipped_responses.append(resp)

        if not ready:
            # Cyclic/unsatisfied dependencies: mark remaining as blocked to avoid silent drops.
            for s in remaining:
                if s.id in completed_ok or s.id in completed_fail:
                    continue
                deps = [d for d in (s.depends_on or []) if d in ids and d != s.id]
                corr_id = f"{session_id}:{s.id}"
                resp = ExecutorResponse(
                    task_id=s.id,
                    status="blocked",
                    summary=f"⛔ Шаг {s.id} пропущен: не удалось удовлетворить зависимости (возможен цикл): {', '.join(deps) or 'none'}.",
                    outputs=[],
                    tool_calls=[{"tool": "orchestrator", "error": "unsatisfied_dependencies", "corr_id": corr_id, "depends_on": deps}],
                    next_questions=[],
                )
                validate_response(resp)
                skipped_responses.append(resp)
                completed_fail.add(s.id)
            return [], skipped_responses

        # Validate parallelizable: require reason and be conservative for file-mutating instructions.
        for s in ready:
            if s.parallelizable:
                reason = (s.parallelizable_reason or "").strip()
                if not reason:
                    s.parallelizable = False
                    continue
                instr = (s.instruction or "").lower()
                risky = any(k in instr for k in ["write_file", "edit_file", "delete_file", "send_file", "git", "commit", "push", "merge", "rebase"])
                if risky and "read" not in reason.lower() and "только чтение" not in reason.lower():
                    s.parallelizable = False

        # Prefer one parallel group if available, else single step in original order.
        order = [s.id for s in steps]
        groups: Dict[str, List[PlanStep]] = {}
        singles: List[PlanStep] = []
        for s in ready:
            if s.parallel_group and s.parallelizable:
                groups.setdefault(s.parallel_group, []).append(s)
            else:
                singles.append(s)
        if groups:
            for sid in order:
                s = next((x for x in ready if x.id == sid), None)
                if not s:
                    continue
                gid = s.parallel_group
                if gid and gid in groups:
                    return groups[gid], skipped_responses
        # fall back to first single in stable order
        singles_set = {s.id for s in singles}
        for sid in order:
            if sid in singles_set:
                return [next(x for x in singles if x.id == sid)], skipped_responses
        return [singles[0]], skipped_responses

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
        self._log.info("step start corr_id=%s step_type=%s profile=%s allowed_tools=%s",
                       corr_id, step.step_type, profile.name,
                       ",".join(profile.allowed_tools[:5]) + ("..." if len(profile.allowed_tools) > 5 else ""))
        self._log.info("step instruction: %s", (step.instruction or "")[:300])
        resp: ExecutorResponse = await self._executor.run(session, req, bot, context, dest, profile)
        self._log.info("step end corr_id=%s status=%s summary=%s", corr_id,
                       getattr(resp, "status", None), (getattr(resp, "summary", "") or "")[:200])
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
            self._log.info("memory: no update needed")
            return
        tag, content = decision
        self._log.info("memory: saving tag=%s content_len=%d", tag, len(content))
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
