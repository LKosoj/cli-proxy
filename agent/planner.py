from __future__ import annotations

import json
import time
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
      "ask_question": null,
      "ask_options": null
    }
  ]
}
Правила:
- Не общайся с пользователем напрямую.
- Если нужно уточнение, добавь шаг с step_type="ask_user" и заполни ask_question + ask_options (минимум 2 варианта).
- Шаги должны быть исполнимы исполнителем с инструментами.
- Если параллельность не нужна, оставляй parallel_group = null.
"""


async def plan_steps(config: AppConfig, user_message: str, context: str) -> List[PlanStep]:
    raw = await chat_completion(
        config,
        _PLANNER_SYSTEM,
        f"Контекст:\n{context}\n\nЗапрос пользователя:\n{user_message}",
    )
    if not raw:
        return [PlanStep(id="step1", title="Выполнить задачу", instruction=user_message)]
    try:
        payload = json.loads(raw)
        steps_raw = payload.get("steps", [])
    except Exception:
        return [PlanStep(id="step1", title="Выполнить задачу", instruction=user_message)]
    steps: List[PlanStep] = []
    for idx, item in enumerate(steps_raw, start=1):
        step_id = item.get("id") or f"step{idx}"
        step = PlanStep(
            id=step_id,
            title=item.get("title") or f"Шаг {idx}",
            instruction=item.get("instruction") or user_message,
            step_type=item.get("step_type") or "task",
            parallel_group=item.get("parallel_group"),
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
    return steps
