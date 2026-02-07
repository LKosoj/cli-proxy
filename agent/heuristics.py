from __future__ import annotations


from config import AppConfig
from .contracts import PlanStep


def needs_clarification(text: str, config: AppConfig) -> bool:
    if not config.defaults.clarification_enabled:
        return False
    message = (text or "").lower()
    if "?" in message:
        return True
    for kw in config.defaults.clarification_keywords:
        if kw and kw in message:
            return True
    return False


def normalize_ask_step(step: PlanStep) -> None:
    if not step.ask_question:
        step.ask_question = "Нужно уточнение. Можете уточнить детали?"
    if not step.ask_options or len(step.ask_options) < 2:
        step.ask_options = ["Продолжить с предположениями", "Уточню сейчас"]
