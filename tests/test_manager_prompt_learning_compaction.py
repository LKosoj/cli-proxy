from __future__ import annotations

import asyncio
import json
import types

import yaml

from agent.manager import ManagerOrchestrator, _archive_response_write
from app.services.project_prompts_service import ensure_project_prompts


def _make_orchestrator() -> ManagerOrchestrator:
    obj = object.__new__(ManagerOrchestrator)
    obj._config = types.SimpleNamespace(defaults=types.SimpleNamespace())
    return obj


def test_apply_manager_prompt_learning_uses_all_patches(tmp_path) -> None:
    orch = _make_orchestrator()
    ensure_project_prompts(str(tmp_path))
    learning_path = tmp_path / ".cli-proxy" / ".manager" / "prompt" / "learning.yaml"
    payload = {
        "patches": [
            {
                "added_rules": f"rule_{i}",
                "changed_rules": "",
                "removed_rules": "",
                "reason": f"reason_{i}",
                "expected_effect": "",
            }
            for i in range(1, 26)
        ],
        "active_version": 1,
    }
    learning_path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")

    rendered = orch._apply_manager_prompt_learning(str(tmp_path), "BASE")
    assert "- rule_1\n" not in rendered
    assert "- rule_6\n" in rendered
    assert "- rule_25\n" in rendered


def test_manager_prompt_learning_compacts_when_patches_exceed_threshold(tmp_path, monkeypatch) -> None:
    async def _run() -> None:
        orch = _make_orchestrator()
        ensure_project_prompts(str(tmp_path))

        async def _fake_compact(_workdir, _patches):
            return {
                "added_rules": ["compact_rule"],
                "changed_rules": [],
                "removed_rules": [],
                "reason": "compacted",
                "expected_effect": "stable",
            }

        monkeypatch.setattr(orch, "_compact_manager_prompt_patches_llm", _fake_compact)

        for i in range(21):
            audit_result = {
                "manager_prompt_patch_candidate": {
                    "added_rules": [f"r{i}"],
                    "changed_rules": [],
                    "removed_rules": [],
                    "reason": f"reason_{i}",
                    "expected_effect": "",
                }
            }
            await orch._learn_from_final_spec_audit(
                workdir=str(tmp_path),
                original_goal="goal",
                audit_result=audit_result,
            )

        learning_path = tmp_path / ".cli-proxy" / ".manager" / "prompt" / "learning.yaml"
        data = yaml.safe_load(learning_path.read_text(encoding="utf-8"))
        patches = data.get("patches") or []
        assert len(patches) == 1
        assert patches[0].get("added_rules") == ["compact_rule"]

    asyncio.run(_run())


def test_archive_response_write_uses_manager_response_subdir(tmp_path) -> None:
    _archive_response_write(str(tmp_path), "manager_archive_test", "Archive Title", "Archive Body")

    response_dir = tmp_path / ".cli-proxy" / ".manager" / "response"
    assert response_dir.exists()
    files = list(response_dir.glob("*_manager_archive_test.md"))
    assert len(files) == 1
    assert "Archive Body" in files[0].read_text(encoding="utf-8")


def test_build_manager_prompt_patch_handles_string_rules_without_char_split(monkeypatch, tmp_path) -> None:
    async def _run() -> None:
        orch = _make_orchestrator()
        ensure_project_prompts(str(tmp_path))

        async def _fake_chat_completion(_cfg, _system, _user, **_kwargs):
            return json.dumps(
                {
                    "added_rules": "Всегда проверяй coverage REQ-*",
                    "changed_rules": ["Уточняй acceptance criteria"],
                    "removed_rules": "",
                    "reason": "стабилизация плана",
                    "expected_effect": "меньше пропусков",
                },
                ensure_ascii=False,
            )

        monkeypatch.setattr("agent.manager_core.chat_completion", _fake_chat_completion)
        patch = await orch._build_manager_prompt_patch_from_final_audit(
            original_goal="goal",
            audit_result={"status": "GAP_FIXED"},
            workdir=str(tmp_path),
        )

        assert patch is not None
        assert patch["added_rules"] == ["Всегда проверяй coverage REQ-*"]
        assert patch["changed_rules"] == ["Уточняй acceptance criteria"]
        assert patch["removed_rules"] == []

    asyncio.run(_run())


def test_learn_from_final_audit_discards_task_specific_patch_rules(tmp_path) -> None:
    async def _run() -> None:
        orch = _make_orchestrator()
        ensure_project_prompts(str(tmp_path))
        await orch._learn_from_final_spec_audit(
            workdir=str(tmp_path),
            original_goal="goal",
            audit_result={
                "manager_prompt_patch_candidate": {
                    "added_rules": [
                        "Для RQ-06 обязательно проверять повторный показ pending после обработки head.",
                        "Всегда подтверждай каждое требование проверяемым evidence.",
                    ],
                    "changed_rules": ["Для task_8 обязательно добавлять отдельную проверку cancel."],
                    "removed_rules": [],
                    "reason": "Пробел по RQ-06.4 на конкретной задаче",
                    "expected_effect": "Снизится риск пропуска RQ-06 в будущем",
                }
            },
        )

        learning_path = tmp_path / ".cli-proxy" / ".manager" / "prompt" / "learning.yaml"
        data = yaml.safe_load(learning_path.read_text(encoding="utf-8"))
        patches = data.get("patches") or []
        assert len(patches) == 1
        patch = patches[0]
        assert patch["added_rules"] == ["Всегда подтверждай каждое требование проверяемым evidence."]
        assert patch["changed_rules"] == []
        assert patch["reason"] == ""
        assert patch["expected_effect"] == ""

    asyncio.run(_run())


def test_build_manager_prompt_patch_filters_task_specific_rules(monkeypatch, tmp_path) -> None:
    async def _run() -> None:
        orch = _make_orchestrator()
        ensure_project_prompts(str(tmp_path))

        async def _fake_chat_completion(_cfg, _system, _user, **_kwargs):
            return json.dumps(
                {
                    "added_rules": [
                        "Для RQ-08 всегда проверяй ветку ask_user exception.",
                        "Перед завершением проверяй все ветки ошибок.",
                    ],
                    "changed_rules": [],
                    "removed_rules": [],
                    "reason": "Устраняем пропуск по task_3",
                    "expected_effect": "Меньше пропусков по RQ-08",
                },
                ensure_ascii=False,
            )

        monkeypatch.setattr("agent.manager_core.chat_completion", _fake_chat_completion)
        patch = await orch._build_manager_prompt_patch_from_final_audit(
            original_goal="goal",
            audit_result={"status": "GAP_FIXED"},
            workdir=str(tmp_path),
        )

        assert patch is not None
        assert patch["added_rules"] == ["Перед завершением проверяй все ветки ошибок."]
        assert patch["reason"] == ""
        assert patch["expected_effect"] == ""

    asyncio.run(_run())
