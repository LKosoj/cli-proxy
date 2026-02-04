from __future__ import annotations

import json
import time
from typing import List

import requests

from .contracts import PlanStep
from .heuristics import needs_clarification, normalize_ask_step
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


def _get_openai_config(config: AppConfig):
    api_key = config.defaults.openai_api_key
    model = config.defaults.openai_model
    base_url = config.defaults.openai_base_url or "https://api.openai.com"
    if not api_key or not model:
        return None
    return api_key, model, base_url.rstrip("/")


def _call_planner_llm(config: AppConfig, user_message: str, context: str) -> str:
    cfg = _get_openai_config(config)
    if not cfg:
        return ""
    api_key, model, base_url = cfg
    messages = [
        {"role": "system", "content": _PLANNER_SYSTEM},
        {"role": "user", "content": f"Контекст:\n{context}\n\nЗапрос пользователя:\n{user_message}"},
    ]
    payload = {"model": model, "messages": messages, "temperature": 0.2}
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    resp = requests.post(f"{base_url}/v1/chat/completions", json=payload, headers=headers, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    return (data["choices"][0]["message"].get("content") or "").strip()


def plan_steps(config: AppConfig, user_message: str, context: str) -> List[PlanStep]:
    raw = _call_planner_llm(config, user_message, context)
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
