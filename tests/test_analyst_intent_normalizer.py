import json
import types

import pytest

from modes.analyst.routing_rules import classify_profile_from_text
from modes.analyst.mode import AnalystMode
from modes.analyst.schemas import AnalystIntentOutputSchema, validate_analyst_payload


class _Tooling:
    def __init__(self, output: str, *, success: bool = True) -> None:
        self.output = output
        self.success = success
        self.last_args = None

    async def execute(self, _name, args, _ctx):
        self.last_args = dict(args)
        return {"success": self.success, "output": self.output}


def _build_mode(tooling: _Tooling) -> AnalystMode:
    mode = AnalystMode()
    mode.initialize(
        config=types.SimpleNamespace(),
        services={"tooling": tooling},
    )
    return mode


def _build_ctx() -> tuple[types.SimpleNamespace, dict, types.SimpleNamespace]:
    session = types.SimpleNamespace(
        id="s1",
        analyst_runtime_template_id="",
        analyst_template_id="default",
        project_root="/tmp/project",
        workdir="/tmp/workdir",
    )
    dest = {"chat_id": 1, "chat_type": "private"}
    bot_app = types.SimpleNamespace(config=types.SimpleNamespace(defaults=types.SimpleNamespace()))
    return session, dest, bot_app


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        (lambda payload: payload.pop("analysis_profile"), "analysis_profile"),
        (lambda payload: payload.pop("document_kind"), "document_kind"),
        (lambda payload: payload.pop("detail_level"), "detail_level"),
        (lambda payload: payload.pop("summary"), "summary"),
    ],
)
def test_analyst_intent_output_schema_rejects_missing_required_fields(mutation, match):
    payload = {
        "analysis_profile": "codebase",
        "document_kind": "spec",
        "detail_level": "standard",
        "summary": "Анализ проекта",
    }
    mutation(payload)

    with pytest.raises(ValueError, match=match):
        validate_analyst_payload(payload, AnalystIntentOutputSchema, contract="intent_output")


@pytest.mark.asyncio
async def test_analyst_classify_intent_passes_structured_clarification_answers_to_tool() -> None:
    payload = {
        "analysis_profile": "codebase",
        "document_kind": "spec",
        "detail_level": "standard",
        "summary": "ТЗ на доработку",
    }
    tooling = _Tooling(json.dumps(payload, ensure_ascii=False))
    mode = _build_mode(tooling)
    session, dest, bot_app = _build_ctx()

    await mode._classify_intent(
        session=session,
        user_text="Сделай ТЗ",
        context=None,
        dest=dest,
        bot_app=bot_app,
        clarification_answers=["Только web", "Мобильные пользователи не важны"],
    )

    assert tooling.last_args["user_text"] == "Сделай ТЗ"
    assert tooling.last_args["clarification_answers"] == [
        "Только web",
        "Мобильные пользователи не важны",
    ]


@pytest.mark.asyncio
async def test_analyst_classify_intent_falls_back_to_deterministic_on_invalid_output() -> None:
    tooling = _Tooling("not-json-output")
    mode = _build_mode(tooling)
    session, dest, bot_app = _build_ctx()
    logged: list[str] = []

    def _fake_warning(msg, *args, **kwargs):  # noqa: ANN001, ARG001
        logged.append(str(msg))

    import unittest.mock
    with unittest.mock.patch.object(mode._log, "warning", _fake_warning):
        result = await mode._classify_intent(
            session=session,
            user_text="Проанализируй проект",
            context=None,
            dest=dest,
            bot_app=bot_app,
        )

    # Falls back to deterministic classification
    assert result.get("analysis_profile")
    assert result.get("document_kind")
    assert result.get("detail_level") == "standard"


@pytest.mark.asyncio
async def test_analyst_classify_intent_returns_valid_profile_from_plugin() -> None:
    payload = {
        "analysis_profile": "greenfield",
        "document_kind": "spec",
        "detail_level": "full",
        "summary": "Новый проект с нуля",
        "clarification_questions": ["Какой стек?"],
    }
    tooling = _Tooling(json.dumps(payload, ensure_ascii=False))
    mode = _build_mode(tooling)
    session, dest, bot_app = _build_ctx()

    result = await mode._classify_intent(
        session=session,
        user_text="Создай новый сервис с нуля",
        context=None,
        dest=dest,
        bot_app=bot_app,
    )

    assert result["analysis_profile"] == "greenfield"
    assert result["document_kind"] == "spec"
    assert result["detail_level"] == "full"
    assert result["summary"] == "Новый проект с нуля"
    assert result["clarification_questions"] == ["Какой стек?"]


@pytest.mark.asyncio
async def test_analyst_classify_intent_routes_codex_cli_change_without_project_root() -> None:
    payload = {
        "analysis_profile": "codebase",
        "document_kind": "spec",
        "detail_level": "full",
        "summary": (
            "Нужно ТЗ на доработку существующего функционала переноса "
            "сессий с поддержкой codex CLI."
        ),
        "template_hint": "change_spec",
    }
    tooling = _Tooling(json.dumps(payload, ensure_ascii=False))
    mode = _build_mode(tooling)
    session, dest, bot_app = _build_ctx()
    session.project_root = ""
    logged: list[str] = []

    def _fake_warning(msg, *args, **kwargs):  # noqa: ANN001, ARG001
        logged.append(str(msg))

    import unittest.mock
    with unittest.mock.patch.object(mode._log, "warning", _fake_warning):
        result = await mode._classify_intent(
            session=session,
            user_text="Добавить поддержку codex CLI в перенос сессий",
            context=None,
            dest=dest,
            bot_app=bot_app,
        )

    template_id, _template = mode._resolve_template(result, "Добавить поддержку codex CLI в перенос сессий")
    assert logged == []
    assert result["analysis_profile"] == "codebase"
    assert result["document_kind"] == "spec"
    assert result["detail_level"] == "full"
    assert result["template_hint"] == "change_spec"
    assert template_id == "change_spec"


def test_classify_profile_from_text_treats_workdir_as_repo_signal() -> None:
    assert classify_profile_from_text(
        "Сделай ТЗ на доработку существующего процесса переноса сессий",
        project_root="",
        workdir="/tmp/workdir",
    ) == "codebase"
