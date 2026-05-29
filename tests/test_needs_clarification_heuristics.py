from types import SimpleNamespace

from modes.sdk.runtime.heuristics import needs_clarification


def _cfg(*, enabled: bool = True, keywords: list[str] | None = None):
    if keywords is None:
        keywords = ["уточни", "уточните", "не ясно", "непонятно"]
    return SimpleNamespace(defaults=SimpleNamespace(clarification_enabled=enabled, clarification_keywords=keywords))


def test_needs_clarification_true_on_explicit_keywords():
    cfg = _cfg()
    assert needs_clarification("Уточни, пожалуйста, детали", cfg) is True
    assert needs_clarification("Мне не ясно что делать дальше", cfg) is True


def test_needs_clarification_false_on_regular_question():
    cfg = _cfg()
    assert needs_clarification("Какой курс доллара сегодня?", cfg) is False


def test_needs_clarification_false_after_user_answer_appended():
    cfg = _cfg(keywords=["уточни", "какой"])
    # Даже если keywords слишком широкие, после уже полученного ответа пользователя
    # не должны вставлять "общий" ask_user повторно.
    text = "Какой курс доллара сегодня?\nОтвет пользователя: User selected: Да, продолжай"
    assert needs_clarification(text, cfg) is False
