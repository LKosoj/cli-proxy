from __future__ import annotations

import logging
from typing import Awaitable, Callable, List, Optional

from config import AppConfig

from .ask_user_schema import apply_ask_schema
from .json_normalizer import loads_safe
from .openai_client import chat_completion as _default_chat_completion


ASK_USER_CLARIFICATION_SYSTEM = """Ты формулируешь один уточняющий вопрос пользователю для оркестратора.
Верни строго JSON:
{
  "ask_question": "...",
  "ask_options": ["...", "..."]
}
Правила:
- Вопрос должен быть конкретным: уточняй 1 самый важный недостающий параметр/выбор.
- Вариантов 2-4, взаимоисключающие, короткие.
- Каждый вариант ответа должен быть короче 60 символов.
- Не используй варианты "Да/Нет" без привязки к содержанию.
- Не используй внутренние технические термины рантайма, пайплайна или репозитория.
- Если seed question сформулирован внутренним языком, перепиши его в понятный пользователю вопрос без потери смысла.
- Не спрашивай про то, что можно определить из контекста.
- Если в контексте уже есть clarification_question/clarification_options, используй их как основной seed.
- Если в контексте есть required_inputs, выбери один обязательный вход и задай вопрос именно про него.
"""


ChatCompletionFn = Callable[..., Awaitable[str]]


async def build_validated_ask_payload(
    config: AppConfig,
    *,
    user_prompt: str,
    system_prompt: str = ASK_USER_CLARIFICATION_SYSTEM,
    chat_completion_fn: Optional[ChatCompletionFn] = None,
    max_attempts: int = 3,
    log: Optional[logging.Logger] = None,
    log_prefix: str = "ask_user",
) -> tuple[str, List[str]]:
    completion = chat_completion_fn or _default_chat_completion
    logger = log or logging.getLogger(__name__)
    base_prompt = str(user_prompt or "").strip()
    if not base_prompt:
        raise RuntimeError("ask_user generation prompt is empty")

    retry_note = ""
    last_error = "empty_generation_result"
    for attempt in range(1, max_attempts + 1):
        prompt = base_prompt
        if retry_note:
            prompt = f"{base_prompt}\n\n{retry_note}"
        try:
            raw = await completion(
                config,
                system_prompt,
                prompt,
                response_format={"type": "json_object"},
            )
        except Exception as exc:
            last_error = f"llm_call_failed:{exc}"
            logger.warning(
                "%s: ask payload generation failed on attempt %d/%d: %s",
                log_prefix,
                attempt,
                max_attempts,
                exc,
            )
            retry_note = (
                "Предыдущая попытка не удалась из-за ошибки вызова модели. "
                "Верни только JSON с ask_question и ask_options."
            )
            continue

        try:
            payload = loads_safe(raw, strict_first=False)
        except Exception as exc:
            last_error = f"json_parse_failed:{exc}"
            logger.warning(
                "%s: ask payload JSON parse failed on attempt %d/%d: %s",
                log_prefix,
                attempt,
                max_attempts,
                exc,
            )
            retry_note = (
                "Предыдущий ответ был невалидным JSON. "
                "Верни только JSON-объект с ask_question и ask_options."
            )
            continue

        if not isinstance(payload, dict):
            last_error = "non_object_payload"
            logger.warning(
                "%s: ask payload is not a JSON object on attempt %d/%d",
                log_prefix,
                attempt,
                max_attempts,
            )
            retry_note = (
                "Предыдущий ответ был не JSON-объектом. "
                "Верни только JSON-объект с ask_question и ask_options."
            )
            continue

        question, options, issues = apply_ask_schema(
            str(payload.get("ask_question") or "").strip(),
            payload.get("ask_options") or [],
        )
        if not issues:
            return question, options

        last_error = ",".join(issues)
        logger.warning(
            "%s: invalid ask payload on attempt %d/%d: issues=%s question=%r options=%s",
            log_prefix,
            attempt,
            max_attempts,
            issues,
            question[:120],
            options,
        )
        retry_note = (
            "Предыдущий вариант не прошел валидацию. "
            f"Ошибки: {', '.join(issues)}. "
            "Исправь вопрос и варианты. Нужен один понятный пользователю вопрос и 2-4 коротких варианта ответа."
        )

    raise RuntimeError(
        f"Не удалось сгенерировать валидный ask_user payload после {max_attempts} попыток: {last_error}"
    )
