from __future__ import annotations

import asyncio
import json
import types

import pytest
import yaml

import agent.manager as manager_mod
from agent.manager import ManagerOrchestrator
from app.services.project_prompts_service import ensure_project_prompts
from modes.sdk.runtime.cli_contracts import CLIResponseFormat
from modes.sdk.runtime.contracts import DevTask, ProjectPlan


class _FakeBot:
    def __init__(self) -> None:
        self.messages = []

    async def _send_message(self, _context, *, chat_id: int, text: str, **_kwargs) -> None:
        self.messages.append((chat_id, text))


def _make_orchestrator() -> ManagerOrchestrator:
    obj = object.__new__(ManagerOrchestrator)
    obj._config = types.SimpleNamespace(
        defaults=types.SimpleNamespace(
            manager_dev_timeout_sec=10,
            manager_max_attempts=2,
            manager_response_archive=False,
        )
    )
    return obj


def test_final_tz_audit_learns_patch_when_gap_was_fixed(monkeypatch, tmp_path) -> None:
    async def _run() -> None:
        orch = _make_orchestrator()
        session = types.SimpleNamespace(workdir=str(tmp_path), manager_quiet_mode=False)
        ensure_project_prompts(str(tmp_path))
        plan = ProjectPlan(
            project_goal="goal",
            tasks=[DevTask(id="t1", title="T", description="D", acceptance_criteria=["ok"])],
            status="completed",
        )
        bot = _FakeBot()

        payload = {
            "status": "GAP_FIXED",
            "summary": "Закрыт пропуск по ТЗ",
            "gaps_found": ["Нет проверки X"],
            "fixes_applied": [
                {
                    "gap": "Нет проверки X",
                    "changes": "Добавлен тест tests/test_x.py",
                    "evidence": "pytest tests/test_x.py::test_x passed",
                }
            ],
            "remaining_gaps": [],
            "tests": [],
            "lint": [],
            "requirement_matrix": [
                {
                    "req_id": "REQ-1",
                    "status": "PASS",
                    "tasks": ["task_1"],
                    "evidence": ["pytest tests/test_x.py::test_x passed"],
                    "gap": "",
                }
            ],
            "manager_prompt_patch_candidate": {
                "added_rules": ["Всегда добавляй отдельную задачу на проверку X"],
                "changed_rules": [],
                "removed_rules": [],
                "reason": "X системно пропускается",
                "expected_effect": "X не будет теряться",
            },
        }

        async def fake_routed(*_args, **_kwargs):
            assert str(_kwargs.get("response_format") or "") == CLIResponseFormat.JSON_OBJECT
            return "codex", json.dumps(payload, ensure_ascii=False)

        monkeypatch.setattr(manager_mod, "run_prompt_routed_meta", fake_routed)

        result = await orch._run_final_spec_audit_and_close_gaps(
            session=session,
            plan=plan,
            bot=bot,
            context=None,
            dest={"chat_id": 1, "kind": "telegram"},
            original_goal="Сделать X",
        )

        assert result["passed"] is True
        assert "доработок внесено: 1" in result["summary_text"]

        learning_path = tmp_path / ".cli-proxy" / ".manager" / "prompt" / "learning.yaml"
        assert learning_path.exists()
        data = yaml.safe_load(learning_path.read_text(encoding="utf-8"))
        assert isinstance(data.get("patches"), list)
        assert data["patches"]

    asyncio.run(_run())


def test_final_tz_audit_fails_when_gaps_remain_after_retries(monkeypatch, tmp_path) -> None:
    async def _run() -> None:
        orch = _make_orchestrator()
        session = types.SimpleNamespace(workdir=str(tmp_path), manager_quiet_mode=False)
        ensure_project_prompts(str(tmp_path))
        plan = ProjectPlan(
            project_goal="goal",
            tasks=[DevTask(id="t1", title="T", description="D", acceptance_criteria=["ok"])],
            status="completed",
        )
        bot = _FakeBot()
        calls = {"n": 0}

        payload = {
            "status": "FAIL",
            "summary": "Остались gap",
            "gaps_found": ["A"],
            "fixes_applied": [],
            "remaining_gaps": ["A"],
            "tests": [],
            "lint": [],
            "requirement_matrix": [
                {
                    "req_id": "REQ-1",
                    "status": "FAIL",
                    "tasks": [],
                    "evidence": [],
                    "gap": "A",
                }
            ],
        }

        async def fake_routed(*_args, **_kwargs):
            calls["n"] += 1
            return "codex", json.dumps(payload, ensure_ascii=False)

        monkeypatch.setattr(manager_mod, "run_prompt_routed_meta", fake_routed)

        result = await orch._run_final_spec_audit_and_close_gaps(
            session=session,
            plan=plan,
            bot=bot,
            context=None,
            dest={"chat_id": 1, "kind": "telegram"},
            original_goal="Сделать X",
        )

        assert calls["n"] == 2
        assert result["passed"] is False
        assert "осталось gap: 1" in result["summary_text"]

    asyncio.run(_run())


def test_final_tz_audit_retries_after_invalid_json_then_passes(monkeypatch, tmp_path) -> None:
    async def _run() -> None:
        orch = _make_orchestrator()
        session = types.SimpleNamespace(workdir=str(tmp_path), manager_quiet_mode=False)
        ensure_project_prompts(str(tmp_path))
        plan = ProjectPlan(
            project_goal="goal",
            tasks=[DevTask(id="t1", title="T", description="D", acceptance_criteria=["ok"])],
            status="completed",
        )
        bot = _FakeBot()
        calls = {"n": 0}
        payload = {
            "status": "PASS",
            "summary": "ok",
            "gaps_found": [],
            "fixes_applied": [],
            "remaining_gaps": [],
            "tests": [],
            "lint": [],
            "requirement_matrix": [],
        }

        async def fake_routed(*_args, **_kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return "codex", "Тесты зависли. Завершу их и запущу заново."
            return "codex", json.dumps(payload, ensure_ascii=False)

        monkeypatch.setattr(manager_mod, "run_prompt_routed_meta", fake_routed)

        result = await orch._run_final_spec_audit_and_close_gaps(
            session=session,
            plan=plan,
            bot=bot,
            context=None,
            dest={"chat_id": 1, "kind": "telegram"},
            original_goal="Сделать X",
        )

        assert calls["n"] == 2
        assert result["passed"] is True
        assert "итог: PASS" in result["summary_text"]

    asyncio.run(_run())


def test_parse_final_tz_audit_raises_on_schema_mismatch_missing_required_fields() -> None:
    orch = _make_orchestrator()
    raw = json.dumps(
        {
            "status": "PASS",
            "summary": "ok",
            # gaps_found intentionally missing
            "fixes_applied": [],
            "remaining_gaps": [],
            "tests": [],
            "lint": [],
            "requirement_matrix": [],
        },
        ensure_ascii=False,
    )

    with pytest.raises(Exception):
        orch._parse_final_spec_audit_json(raw)


def test_parse_final_tz_audit_accepts_json_fence_with_prologue() -> None:
    orch = _make_orchestrator()
    payload = {
        "status": "PASS",
        "summary": "ok",
        "gaps_found": [],
        "fixes_applied": [],
        "remaining_gaps": [],
        "tests": [],
        "lint": [],
        "requirement_matrix": [],
    }
    raw = "Сформирую отчёт:\n```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```"
    parsed = orch._parse_final_spec_audit_json(raw)
    assert parsed["status"] == "PASS"
    assert parsed["summary"] == "ok"


def test_parse_final_tz_audit_injects_empty_lint_when_missing() -> None:
    orch = _make_orchestrator()
    raw = json.dumps(
        {
            "status": "PASS",
            "summary": "ok",
            "gaps_found": [],
            "fixes_applied": [],
            "remaining_gaps": [],
            "tests": [],
            "requirement_matrix": [],
        },
        ensure_ascii=False,
    )
    parsed = orch._parse_final_spec_audit_json(raw)
    assert parsed["status"] == "PASS"
    assert parsed["lint"] == []


def test_parse_final_tz_audit_accepts_nullable_gap_and_patch_candidate() -> None:
    orch = _make_orchestrator()
    raw = json.dumps(
        {
            "status": "PASS",
            "summary": "ok",
            "gaps_found": [],
            "fixes_applied": [],
            "remaining_gaps": [],
            "tests": [],
            "lint": [],
            "requirement_matrix": [
                {
                    "req_id": "REQ-1",
                    "status": "PASS",
                    "tasks": ["task_1"],
                    "evidence": ["pytest -q"],
                    "gap": None,
                }
            ],
            "manager_prompt_patch_candidate": None,
        },
        ensure_ascii=False,
    )

    parsed = orch._parse_final_spec_audit_json(raw)
    assert parsed["requirement_matrix"] == [
        {
            "req_id": "REQ-1",
            "status": "PASS",
            "tasks": ["task_1"],
            "evidence": ["pytest -q"],
            "gap": "",
        }
    ]
    assert parsed["manager_prompt_patch_candidate"] == {}


def test_final_tz_audit_schema_mismatch_blocks_pass(monkeypatch, tmp_path) -> None:
    async def _run() -> None:
        orch = _make_orchestrator()
        session = types.SimpleNamespace(workdir=str(tmp_path), manager_quiet_mode=False)
        ensure_project_prompts(str(tmp_path))
        plan = ProjectPlan(
            project_goal="goal",
            tasks=[DevTask(id="t1", title="T", description="D", acceptance_criteria=["ok"])],
            status="completed",
        )
        bot = _FakeBot()
        calls = {"n": 0}

        invalid_payload = {
            "status": "PASS",
            "summary": "looks passed",
            # Intentionally missing required fields to force strict schema mismatch.
        }

        async def fake_routed(*_args, **_kwargs):
            calls["n"] += 1
            return "codex", json.dumps(invalid_payload, ensure_ascii=False)

        monkeypatch.setattr(manager_mod, "run_prompt_routed_meta", fake_routed)

        result = await orch._run_final_spec_audit_and_close_gaps(
            session=session,
            plan=plan,
            bot=bot,
            context=None,
            dest={"chat_id": 1, "kind": "telegram"},
            original_goal="Сделать X",
        )

        assert calls["n"] == 2
        assert result["passed"] is False
        assert "невалидный JSON" in str(result.get("summary_text") or "")
        details = result.get("result") or {}
        assert str(details.get("status") or "") == "FAIL"

    asyncio.run(_run())
