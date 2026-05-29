from __future__ import annotations

import pytest

from app.services.lint_evolution.canonicalizer import canonicalize
from app.services.lint_evolution.rule_kinds import UNKNOWN


@pytest.mark.parametrize(
    "text,expected_kind",
    [
        ("Тесты не проходят: падает test_decompose_logs_validation", "tests_failing"),
        ("test failed in module x", "tests_failing"),
        ("Ошибка синтаксиса в строке 28 (unmatched ')')", "syntax_error"),
        ("Invalid syntax detected", "syntax_error"),
        ("Найдены заглушки в виде 'pass' с #TODO", "missing_implementation"),
        ("NotImplementedError raised in handler", "missing_implementation"),
        ("В рабочем дереве нет изменений (git status пуст)", "no_evidence_provided"),
        ("Убраны неиспользуемые импорты", "unused_imports"),
        ("Fix analyst mode bootstrap circular import", "circular_dependency"),
        ("Удалены устаревшие SPEC-файлы", "dead_code_present"),
        ("pytest был убит вручную из-за таймаута", "test_timeout"),
        ("Сокращение не соответствует требованию «в несколько раз»", "acceptance_criteria_unmet"),
        ("Потеря запроса с изображениями при активном режиме", "logic_error"),
    ],
)
def test_canonicalize_assigns_expected_kind(text: str, expected_kind: str) -> None:
    result = canonicalize(text)
    assert result.rule_kind == expected_kind


def test_unknown_when_no_pattern_matches() -> None:
    result = canonicalize("совершенно нейтральный текст без сигналов")
    assert result.rule_kind == UNKNOWN
    assert result.subject_hash


def test_subject_hash_is_stable_for_same_text() -> None:
    a = canonicalize("Тесты не проходят: error A")
    b = canonicalize("Тесты не проходят: error A")
    assert a.subject_hash == b.subject_hash


def test_subject_hash_differs_for_different_text() -> None:
    a = canonicalize("Тесты не проходят: error A в модуле x")
    b = canonicalize("Тесты не проходят: error B в модуле y")
    assert a.subject_hash != b.subject_hash
