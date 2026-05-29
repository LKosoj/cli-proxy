from __future__ import annotations

import re
from typing import List, Tuple


PLACEHOLDER_OPTIONS = {"a", "b", "c", "d", "1", "2", "3", "4"}
DEFAULT_ASK_QUESTION = "Нужно уточнение. Можете уточнить детали?"
DEFAULT_ASK_OPTIONS = ["Продолжить с предположениями", "Нужны дополнительные данные"]
NON_SEMANTIC_ASK_ANSWERS = [
    *DEFAULT_ASK_OPTIONS,
    "Продолжить с допущениями",
    "Остановиться и уточнить",
]
_NON_SEMANTIC_ASK_ANSWERS_CASEFOLDED = {
    str(item).strip().casefold()
    for item in NON_SEMANTIC_ASK_ANSWERS
    if str(item).strip()
}


def normalize_ask_question(question: str) -> str:
    return re.sub(r"[ \t]+\n", "\n", re.sub(r"[ \t]+", " ", str(question or "").strip()))


def normalize_ask_options(options: List[str] | Tuple[str, ...] | None) -> List[str]:
    deduped: List[str] = []
    seen: set[str] = set()
    for item in options or []:
        text = str(item or "").strip()
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(text)
    return deduped[:4]


def is_non_semantic_ask_answer(answer: str) -> bool:
    text = normalize_ask_question(answer)
    if not text:
        return False
    return text.casefold() in _NON_SEMANTIC_ASK_ANSWERS_CASEFOLDED


def validate_ask_payload(question: str, options: List[str]) -> List[str]:
    issues: List[str] = []
    normalized_question = normalize_ask_question(question)
    normalized_options = normalize_ask_options(options)
    if not normalized_question:
        issues.append("empty_question")
    if len(normalized_question) > 240:
        issues.append("question_too_long")
    if normalized_question.count("?") > 1:
        issues.append("multiple_question_marks")
    if len(re.findall(r"(?:^|\n)\s*\d+[.)]\s+", normalized_question)) >= 2:
        issues.append("multi_aspect_question")
    if len(re.findall(r"(?:^|\n)\s*[-*•]\s+", normalized_question)) >= 2:
        issues.append("multi_aspect_question")
    if normalized_question.count("**") >= 4:
        issues.append("multi_aspect_question")
    if len(normalized_options) < 2:
        issues.append("too_few_options")
    if len(normalized_options) != len(options or []):
        issues.append("options_normalized")
    if normalized_options and all(opt.lower() in PLACEHOLDER_OPTIONS for opt in normalized_options[:4]):
        issues.append("placeholder_options")
    if any(len(opt) > 80 for opt in normalized_options):
        issues.append("option_too_long")
    return list(dict.fromkeys(issues))


def apply_ask_schema(question: str, options: List[str] | Tuple[str, ...] | None) -> tuple[str, List[str], List[str]]:
    normalized_question = normalize_ask_question(question)
    normalized_options = normalize_ask_options(options)
    issues = validate_ask_payload(normalized_question, normalized_options)
    if "placeholder_options" in issues:
        normalized_options = []
    return normalized_question, normalized_options[:4], issues
