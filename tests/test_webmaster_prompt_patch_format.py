from __future__ import annotations

import asyncio
import json
import types

import yaml

from app.services.project_prompts_service import ensure_project_prompts
from modes.webmaster.mode import WebmasterMode


def test_webmaster_parse_llm_json_accepts_fenced_payload() -> None:
    mode = WebmasterMode()
    payload = mode._parse_llm_json(
        "text\n```json\n{\"kind\":\"wrong_execution\",\"reason\":\"r\"}\n```\nend",
        required_fields=("kind", "reason"),
    )
    assert payload["kind"] == "wrong_execution"
    assert payload["reason"] == "r"


def test_webmaster_mode_has_no_shared_prompts_workdir_state() -> None:
    mode = WebmasterMode()
    assert not hasattr(mode, "_prompts_workdir")


def test_webmaster_build_prompt_patch_returns_rule_lists(monkeypatch, tmp_path) -> None:
    async def _run() -> None:
        mode = WebmasterMode()
        ensure_project_prompts(str(tmp_path))
        session = types.SimpleNamespace(workdir=str(tmp_path))

        async def _fake_chat_completion(_self, _bot_app, _system, _user, **_kwargs):
            return json.dumps(
                {
                    "added_rules": "Проверяй REQ coverage",
                    "changed_rules": ["Не пропускай checklist"],
                    "removed_rules": "",
                    "reason": "стабильность",
                    "expected_effect": "меньше ошибок",
                },
                ensure_ascii=False,
            )

        mode._chat_completion = types.MethodType(_fake_chat_completion, mode)
        patch = await mode._build_prompt_patch_llm(
            types.SimpleNamespace(),
            "feedback",
            "report",
            session=session,
        )
        assert patch["added_rules"] == ["Проверяй REQ coverage"]
        assert patch["changed_rules"] == ["Не пропускай checklist"]
        assert patch["removed_rules"] == []

    asyncio.run(_run())


def test_webmaster_build_prompt_patch_uses_session_workdir_per_call(tmp_path) -> None:
    async def _run() -> None:
        mode = WebmasterMode()
        workdir_a = tmp_path / "a"
        workdir_b = tmp_path / "b"
        workdir_a.mkdir(parents=True, exist_ok=True)
        workdir_b.mkdir(parents=True, exist_ok=True)
        ensure_project_prompts(str(workdir_a))
        ensure_project_prompts(str(workdir_b))

        prompts_a = workdir_a / ".cli-proxy" / ".webmaster" / "prompt" / "prompts.yaml"
        prompts_b = workdir_b / ".cli-proxy" / ".webmaster" / "prompt" / "prompts.yaml"

        with open(prompts_a, "r", encoding="utf-8") as f:
            payload_a = yaml.safe_load(f) or {}
        with open(prompts_b, "r", encoding="utf-8") as f:
            payload_b = yaml.safe_load(f) or {}
        payload_a.setdefault("prompts", {})["prompt_patch"] = "PATCH_PROMPT_A"
        payload_b.setdefault("prompts", {})["prompt_patch"] = "PATCH_PROMPT_B"
        with open(prompts_a, "w", encoding="utf-8") as f:
            yaml.safe_dump(payload_a, f, allow_unicode=True, sort_keys=False)
        with open(prompts_b, "w", encoding="utf-8") as f:
            yaml.safe_dump(payload_b, f, allow_unicode=True, sort_keys=False)

        captured_patch_prompts: list[str] = []

        async def _fake_chat_completion(_self, _bot_app, _system, user, **_kwargs):
            payload = json.loads(str(user))
            captured_patch_prompts.append(str(payload.get("patch_prompt") or ""))
            return json.dumps(
                {
                    "added_rules": [],
                    "changed_rules": [],
                    "removed_rules": [],
                    "reason": "ok",
                    "expected_effect": "ok",
                },
                ensure_ascii=False,
            )

        mode._chat_completion = types.MethodType(_fake_chat_completion, mode)

        await mode._build_prompt_patch_llm(
            types.SimpleNamespace(),
            "feedback-a",
            "report-a",
            session=types.SimpleNamespace(workdir=str(workdir_a)),
        )
        await mode._build_prompt_patch_llm(
            types.SimpleNamespace(),
            "feedback-b",
            "report-b",
            session=types.SimpleNamespace(workdir=str(workdir_b)),
        )

        assert captured_patch_prompts == ["PATCH_PROMPT_A", "PATCH_PROMPT_B"]

    asyncio.run(_run())


def test_webmaster_build_prompt_patch_drops_task_specific_rules(monkeypatch, tmp_path) -> None:
    async def _run() -> None:
        mode = WebmasterMode()
        ensure_project_prompts(str(tmp_path))
        session = types.SimpleNamespace(workdir=str(tmp_path))

        async def _fake_chat_completion(_self, _bot_app, _system, _user, **_kwargs):
            return json.dumps(
                {
                    "added_rules": [
                        "Для RQ-06 добавляй отдельный пункт проверки head очереди.",
                        "Перед завершением проверяй все ветки ошибок.",
                    ],
                    "changed_rules": [],
                    "removed_rules": [],
                    "reason": "Пропуск по task_7",
                    "expected_effect": "Снижение пропусков по RQ-06",
                },
                ensure_ascii=False,
            )

        mode._chat_completion = types.MethodType(_fake_chat_completion, mode)
        patch = await mode._build_prompt_patch_llm(
            types.SimpleNamespace(),
            "feedback",
            "report",
            session=session,
        )
        assert patch is not None
        assert patch["added_rules"] == ["Перед завершением проверяй все ветки ошибок."]
        assert patch["reason"] == ""
        assert patch["expected_effect"] == ""

    asyncio.run(_run())
