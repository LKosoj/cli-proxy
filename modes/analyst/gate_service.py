from __future__ import annotations

import os
import logging
from typing import Awaitable, Callable

from modes.analyst.run_directory import AnalystRunDirectory

logger = logging.getLogger(__name__)

# Profiles that support Gate 2 (context clarification after data gathering)
_GATE2_PROFILES = frozenset({"codebase", "codebase_plus_research", "audit"})


class AnalystGateService:
    """Управляет gate-точками для уточняющих вопросов пользователю.

    Gate 1 — уточнение постановки (до начала работы).
    Gate 2 — уточнение по найденному контексту (после сбора данных).
    После обоих gate вопросы пользователю запрещены (lock).

    Зависит только от ``AnalystRunDirectory`` — никаких прямых зависимостей
    от session, bot_app, tooling.  Конкретный механизм ask_user передаётся
    как ``ask_fn`` (callable: question → answer).
    """

    GATE1_MAX_QUESTIONS = 3
    GATE2_MAX_QUESTIONS = 2

    def __init__(self, run_dir: AnalystRunDirectory) -> None:
        self._run_dir = run_dir

    # ------------------------------------------------------------------
    # Gate 1: уточнение постановки
    # ------------------------------------------------------------------

    def gate1_should_activate(self, clarification_questions: list[str]) -> bool:
        """Gate 1 активируется детерминированно: *clarification_questions* непустой.

        Фильтрует пустые строки — ``["", " "]`` считается пустым списком.
        """
        return any(q and q.strip() for q in (clarification_questions or []))

    def gate1_questions(self, clarification_questions: list[str]) -> list[str]:
        """Возвращает до ``GATE1_MAX_QUESTIONS`` вопросов для Gate 1."""
        cleaned = [
            q.strip() for q in (clarification_questions or []) if q and q.strip()
        ]
        return cleaned[: self.GATE1_MAX_QUESTIONS]

    async def execute_gate1(
        self,
        questions: list[str],
        ask_fn: Callable[[str], Awaitable[str]],
    ) -> list[str]:
        """Выполняет Gate 1.

        - Задаёт вопросы по одному через *ask_fn*.
        - Сохраняет ответы в run_dir (``append_clarification_answers``).
        - Обновляет ``current_phase`` → ``"gate1"`` в meta.json.
        - Возвращает список ответов.
        """
        if not questions:
            return []

        self._run_dir.update_phase("gate1")

        answers: list[str] = []
        for question in questions:
            answer = str(await ask_fn(question) or "").strip()
            if not answer:
                raise RuntimeError("Gate 1: пустой ответ на уточняющий вопрос")
            answers.append(answer)

        if answers:
            self._run_dir.append_clarification_answers(answers)

        return answers

    # ------------------------------------------------------------------
    # Gate 2: уточнение по контексту
    # ------------------------------------------------------------------

    def gate2_should_activate(self, analysis_profile: str) -> bool:
        """Gate 2 активируется детерминированно.

        Условия (все должны выполняться):
        - profile in (``codebase``, ``codebase_plus_research``, ``audit``)
        - хотя бы один шаг в meta.steps имеет ``status="partial"``
        - либо найден repo-grounded spec-контекст (codebase_context.md)
        - gate2 ещё не выполнялся (``gate2_executed`` is False)
        """
        if str(analysis_profile or "") not in _GATE2_PROFILES:
            return False

        meta = self._run_dir.load_meta()
        if meta.get("gate2_executed", False):
            return False

        steps: list[dict] = list(meta.get("steps") or [])
        if any(s.get("status") == "partial" for s in steps):
            return True
        if str(meta.get("document_kind") or "").strip() != "spec":
            return False
        return os.path.exists(self._run_dir.codebase_context_path())

    def gate2_build_questions(self) -> list[str]:
        """Формулирует до ``GATE2_MAX_QUESTIONS`` вопросов на основе partial-шагов.

        Читает meta.json → steps → фильтрует partial → берёт gap описание.
        """
        meta = self._run_dir.load_meta()
        steps: list[dict] = list(meta.get("steps") or [])
        partial_steps = [s for s in steps if s.get("status") == "partial"]

        questions: list[str] = []
        for step in partial_steps[: self.GATE2_MAX_QUESTIONS]:
            step_id = step.get("id", "unknown")
            gap = str(step.get("gap", "")).strip()
            if gap:
                questions.append(
                    f"При анализе {step_id} обнаружен пробел: {gap}. "
                    f"Можете уточнить?"
                )
            else:
                questions.append(
                    f"Шаг {step_id} завершился частично. "
                    f"Можете предоставить дополнительную информацию?"
                )

        if questions:
            return questions

        return []

    async def execute_gate2(
        self,
        ask_fn: Callable[[str], Awaitable[str]],
    ) -> list[str]:
        """Выполняет Gate 2.

        - Проверяет ``gate2_should_activate``.
        - Если нет — возвращает ``[]``.
        - Строит вопросы через ``gate2_build_questions``.
        - Задаёт через *ask_fn*.
        - Сохраняет ответы.
        - Помечает gate2 как выполненный (``gate2_executed: true``).
        - Обновляет ``current_phase`` → ``"gate2"``.
        - Возвращает список ответов.
        """
        meta = self._run_dir.load_meta()
        profile = str(meta.get("analysis_profile", ""))

        if not self.gate2_should_activate(profile):
            return []

        self._run_dir.update_phase("gate2")

        questions = self.gate2_build_questions()
        if not questions:
            self._run_dir.update_meta(gate2_executed=True)
            return []

        answers: list[str] = []
        for question in questions:
            answer = str(await ask_fn(question) or "").strip()
            if not answer:
                raise RuntimeError("Gate 2: пустой ответ на уточняющий вопрос")
            answers.append(answer)

        if answers:
            self._run_dir.append_clarification_answers(answers)

        self._run_dir.update_meta(gate2_executed=True)

        return answers

    # ------------------------------------------------------------------
    # Lock: запрет вопросов после gates
    # ------------------------------------------------------------------

    def lock_questions(self) -> None:
        """Устанавливает ``ask_user_locked=true`` в meta.json."""
        self._run_dir.update_meta(ask_user_locked=True)

    def is_questions_locked(self) -> bool:
        """Проверяет ``ask_user_locked`` в meta.json."""
        meta = self._run_dir.load_meta()
        return bool(meta.get("ask_user_locked", False))
