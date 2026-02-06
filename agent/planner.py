from __future__ import annotations

import json
import re
import time
import uuid
from typing import List

from .contracts import PlanStep
from .heuristics import needs_clarification, normalize_ask_step
from .openai_client import chat_completion
from config import AppConfig


_PLANNER_SYSTEM = """Ты — оркестратор. Построй план шагов для выполнения задачи пользователя.
Верни строго JSON со структурой:
{
  "steps": [
    {
      "id": "step1",
      "title": "...",
      "instruction": "...",
      "step_type": "task",
      "parallel_group": null,
      "depends_on": [],
      "parallelizable": false,
      "parallelizable_reason": null,
      "ask_question": null,
      "ask_options": null
    }
  ]
}
Правила:
- Не общайся с пользователем напрямую.
- Если нужно уточнение, добавь шаг с step_type="ask_user" и заполни ask_question + ask_options (минимум 2 варианта).
- Шаги должны быть исполнимы исполнителем с инструментами.
- НЕ указывай, какие инструменты использовать исполнителю. Он сам выбирает.
- Параллельность потенциально опасна (гонки по файлам/ресурсам). По умолчанию параллельность выключена.
- Если хочешь запустить шаги параллельно, ОБЯЗАТЕЛЬНО:
  - явно выставь parallelizable=true для каждого шага, который можно исполнять параллельно,
  - объясни почему это безопасно в parallelizable_reason,
  - при необходимости задай parallel_group (одинаковый gid для шагов, которые можно запустить вместе).
- Если есть зависимости, укажи depends_on как список id шагов, которые должны завершиться ДО этого шага.
- Если параллельность не нужна, оставляй parallel_group=null и parallelizable=false.
"""


def _extract_json_object(raw: str) -> str:
    """
    Planner LLM should return strict JSON, but in practice we may get code fences or extra text.
    Try to extract the largest JSON object substring.
    """
    s = raw.strip()
    if not s:
        return s
    # Remove markdown code fences if present.
    s = re.sub(r"^```(?:json)?\\s*", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\\s*```$", "", s)
    # Fast path: looks like JSON already.
    if s.startswith("{") and s.endswith("}"):
        return s
    # Fallback: take substring from first '{' to last '}'.
    i = s.find("{")
    j = s.rfind("}")
    if i >= 0 and j > i:
        return s[i : j + 1]
    return s


async def plan_steps(config: AppConfig, user_message: str, context: str) -> List[PlanStep]:
    raw = await chat_completion(
        config,
        _PLANNER_SYSTEM,
        f"Контекст:\n{context}\n\nЗапрос пользователя:\n{user_message}",
    )
    if not raw:
        return [PlanStep(id="step1", title="Выполнить задачу", instruction=user_message)]
    try:
        payload = json.loads(_extract_json_object(raw))
        steps_raw = payload.get("steps", [])
    except Exception:
        return [PlanStep(id="step1", title="Выполнить задачу", instruction=user_message)]
    steps: List[PlanStep] = []
    if not isinstance(steps_raw, list):
        steps_raw = []
    for idx, item in enumerate(steps_raw, start=1):
        if not isinstance(item, dict):
            continue
        step_id = item.get("id") or f"step{idx}"
        depends_on = item.get("depends_on") or []
        if not isinstance(depends_on, list):
            depends_on = []
        step = PlanStep(
            id=step_id,
            title=item.get("title") or f"Шаг {idx}",
            instruction=item.get("instruction") or user_message,
            step_type=item.get("step_type") or "task",
            parallel_group=item.get("parallel_group"),
            depends_on=[str(x) for x in depends_on if x],
            parallelizable=bool(item.get("parallelizable") or False),
            parallelizable_reason=item.get("parallelizable_reason"),
            ask_question=item.get("ask_question"),
            ask_options=item.get("ask_options"),
        )
        if step.step_type == "ask_user":
            normalize_ask_step(step)
        steps.append(step)
    if not steps:
        steps = [PlanStep(id="step1", title="Выполнить задачу", instruction=user_message)]
    if not any(s.step_type == "ask_user" for s in steps) and needs_clarification(user_message, config):
        ask_step = PlanStep(
            id="ask_user_1",
            title="Уточнение запроса",
            instruction="Запросить уточнение у пользователя",
            step_type="ask_user",
            ask_question="Нужно уточнение по запросу. Продолжить с предположениями?",
            ask_options=["Да, продолжай", "Нет, уточню сейчас"],
        )
        normalize_ask_step(ask_step)
        steps.insert(0, ask_step)
    _ensure_unique_step_ids(steps)
    return steps


def _ensure_unique_step_ids(steps: List[PlanStep]) -> None:
    seen: set[str] = set()
    for idx, step in enumerate(steps, start=1):
        base_id = step.id or f"step{idx}"
        candidate = base_id
        if candidate in seen:
            candidate = f"{base_id}_{int(time.time())}_{uuid.uuid4().hex[:4]}"
        while candidate in seen:
            candidate = f"{base_id}_{uuid.uuid4().hex[:6]}"
        step.id = candidate
        seen.add(candidate)
