import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import modes.sdk.orchestrator_runner as orchestrator_runner_module
from app.services.notification_queue_service import NotificationQueueService
from config import AppConfig, DefaultsConfig, MCPConfig, TelegramConfig
from modes.sdk.services.tasks import TaskService as ModeTaskService
from modes.sdk.orchestrator_runner import OrchestratorRunner
from modes.sdk.runtime.cli_contracts import CLIResponseFormat
from modes.sdk.runtime.contracts import ExecutorResponse, PlanStep
from session import session_runtime_uid
from sessions.conversation_scope import ConversationScope


class _FakeBot:
    def __init__(self):
        self.events = []
        self.sent_outputs = []
        self.sent_docs = []
        self.send_output_called = asyncio.Event()
        self.doc_called = asyncio.Event()

    async def _send_message(self, _context, *, chat_id: int, text: str, **_kwargs):
        self.events.append(("msg", chat_id, text))

    async def send_message(self, _context, *, chat_id: int, text: str, **_kwargs):
        self.events.append(("msg", chat_id, text))

    async def send_output(self, _session, _dest, output: str, _context, **kwargs):
        self.events.append(("send_output", output, kwargs))
        self.sent_outputs.append((output, kwargs))
        self.send_output_called.set()

    async def _send_document(self, _context, *, chat_id: int, document, **_kwargs):
        # document is a file-like object
        self.events.append(("doc", chat_id, getattr(document, "name", "")))
        self.sent_docs.append(getattr(document, "name", ""))
        self.doc_called.set()
        return True


def test_orchestrator_compose_final_answer_sends_ready_and_output(tmp_path, monkeypatch):
    async def _run():
        cfg = AppConfig(
            telegram=TelegramConfig(token="", whitelist_chat_ids=[]),
            tools={},
            defaults=DefaultsConfig(
                workdir=str(tmp_path),
                state_path=str(tmp_path / "state.json"),
                toolhelp_path=str(tmp_path / "toolhelp.json"),
                log_path=str(tmp_path / "bot.log"),
            ),
            mcp=MCPConfig(enabled=False),
            mcp_clients=[],
            presets=[],
            path=str(tmp_path / "config.yaml"),
        )
        orch = OrchestratorRunner(cfg)

        async def _fake_plan_steps(_cfg, _user_text, _ctx):
            return [
                PlanStep(id="step1", title="s1", instruction="do 1"),
                PlanStep(id="step2", title="s2", instruction="do 2", depends_on=["step1"]),
            ]

        monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)
        monkeypatch.setattr(orch, "_missing_required_repo_use_cli_step_ids", lambda *args, **kwargs: [])
        monkeypatch.setattr(orch, "_missing_required_repo_use_cli_step_ids", lambda *args, **kwargs: [])

        async def _fake_execute_step(
            step,
            _session,
            _bot,
            _context,
            _dest,
            _orchestrator_context,
            *,
            current_user_text="",
            constraints=None,
        ):
            return ExecutorResponse(
                task_id=step.id,
                status="ok",
                summary=f"done {step.id}",
                outputs=[{"type": "text", "content": f"out {step.id}"}],
                tool_calls=[{"tool": "fake"}],
                next_questions=[],
            )

        monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)

        async def _noop_memory(*_args, **_kwargs):
            return None

        monkeypatch.setattr(orch, "_maybe_update_memory", _noop_memory)

        async def _fake_chat_completion(_cfg, _system, _user):
            return "FINAL ANSWER"

        monkeypatch.setattr(orch._deps, "chat_completion", _fake_chat_completion)

        fakebot = _FakeBot()
        session = type("S", (), {"id": "s1"})
        dest = {"kind": "telegram", "chat_id": 123, "chat_type": "private"}

        out = await orch.run(session, "do things", bot=fakebot, context=None, dest=dest)
        assert "FINAL ANSWER" in out

        # Ready message + one send_output (HTML+summary is handled inside send_output itself)
        assert fakebot.events[0][0] == "msg"
        assert "Готово" in fakebot.events[0][2]
        await asyncio.wait_for(fakebot.send_output_called.wait(), timeout=1.0)
        assert any(e[0] == "send_output" for e in fakebot.events)
        sent = fakebot.sent_outputs[0]
        assert sent[0] == "FINAL ANSWER"
        assert sent[1].get("send_header") is False
        assert sent[1].get("force_html") is True

    asyncio.run(_run())


def test_orchestrator_compose_final_answer_retries_on_internal_tool_markup_when_structured_output_is_supported(
    tmp_path,
    monkeypatch,
):
    async def _run():
        cfg = AppConfig(
            telegram=TelegramConfig(token="", whitelist_chat_ids=[]),
            tools={},
            defaults=DefaultsConfig(
                workdir=str(tmp_path),
                state_path=str(tmp_path / "state.json"),
                toolhelp_path=str(tmp_path / "toolhelp.json"),
                log_path=str(tmp_path / "bot.log"),
            ),
            mcp=MCPConfig(enabled=False),
            mcp_clients=[],
            presets=[],
            path=str(tmp_path / "config.yaml"),
        )
        orch = OrchestratorRunner(cfg)
        calls = {"n": 0, "formats": []}

        async def _fake_plan_steps(_cfg, _user_text, _ctx):
            return [PlanStep(id="step1", title="s1", instruction="do 1")]

        monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)
        monkeypatch.setattr(orch, "_missing_required_repo_use_cli_step_ids", lambda *args, **kwargs: [])

        async def _fake_execute_step(
            step,
            _session,
            _bot,
            _context,
            _dest,
            _orchestrator_context,
            *,
            current_user_text="",
            constraints=None,
        ):
            _ = current_user_text, constraints
            return ExecutorResponse(
                task_id=step.id,
                status="ok",
                summary="done",
                outputs=[{"type": "text", "content": "out"}],
                tool_calls=[{"tool": "fake"}],
                next_questions=[],
            )

        monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)
        monkeypatch.setattr(orch, "_maybe_update_memory", lambda *_a, **_k: asyncio.sleep(0))

        async def _fake_chat_completion(_cfg, _system, _user, response_format=None, **kwargs):
            calls["n"] += 1
            calls["formats"].append(response_format)
            calls.setdefault("max_tokens", []).append(kwargs.get("max_tokens"))
            if calls["n"] == 1:
                return json.dumps(
                    {
                        "final_text": (
                            "Подготовлю ответ.\n"
                            "[TOOL_CALL]\n"
                            '{tool => "read_file", args => {"path": "/tmp/example"}}\n'
                            "[/TOOL_CALL]"
                        )
                    },
                    ensure_ascii=False,
                )
            return json.dumps({"final_text": "FINAL ANSWER"}, ensure_ascii=False)

        _fake_chat_completion._supports_strict_json_contract = True
        monkeypatch.setattr(orch._deps, "chat_completion", _fake_chat_completion)

        fakebot = _FakeBot()
        session = type("S", (), {"id": "s1"})
        dest = {"kind": "telegram", "chat_id": 123, "chat_type": "private"}

        out = await orch.run(session, "do things", bot=fakebot, context=None, dest=dest)
        assert out == "FINAL ANSWER"
        assert calls["formats"][:2] == [{"type": "json_object"}, {"type": "json_object"}]
        assert calls["max_tokens"][:2] == [32768, 32768]

    asyncio.run(_run())


def test_orchestrator_compose_final_answer_recovers_truncated_final_text_json(tmp_path, monkeypatch):
    async def _run():
        cfg = AppConfig(
            telegram=TelegramConfig(token="", whitelist_chat_ids=[]),
            tools={},
            defaults=DefaultsConfig(
                workdir=str(tmp_path),
                state_path=str(tmp_path / "state.json"),
                toolhelp_path=str(tmp_path / "toolhelp.json"),
                log_path=str(tmp_path / "bot.log"),
            ),
            mcp=MCPConfig(enabled=False),
            mcp_clients=[],
            presets=[],
            path=str(tmp_path / "config.yaml"),
        )
        orch = OrchestratorRunner(cfg)
        captured = {"max_tokens": None, "handler": None}

        async def _fake_plan_steps(_cfg, _user_text, _ctx):
            return [PlanStep(id="step1", title="s1", instruction="do 1")]

        monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)

        async def _fake_execute_step(
            step,
            _session,
            _bot,
            _context,
            _dest,
            _orchestrator_context,
            *,
            current_user_text="",
            constraints=None,
        ):
            _ = step, current_user_text, constraints
            return ExecutorResponse(
                task_id="step1",
                status="ok",
                summary="done",
                outputs=[{"type": "text", "content": "out"}],
                tool_calls=[{"tool": "fake"}],
                next_questions=[],
            )

        monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)
        monkeypatch.setattr(orch, "_maybe_update_memory", lambda *_a, **_k: asyncio.sleep(0))

        async def _fake_chat_completion(_cfg, _system, _user, response_format=None, **kwargs):
            assert response_format == {"type": "json_object"}
            captured["max_tokens"] = kwargs.get("max_tokens")
            captured["handler"] = kwargs.get("normalize_error_handler")
            handler = captured["handler"]
            assert callable(handler)
            broken = '{"final_text":"## Итог\\n\\n- пункт 1'
            return handler(broken, json.JSONDecodeError("Unterminated string", broken, 14))

        _fake_chat_completion._supports_strict_json_contract = True
        monkeypatch.setattr(orch._deps, "chat_completion", _fake_chat_completion)

        fakebot = _FakeBot()
        session = type("S", (), {"id": "s1"})
        dest = {"kind": "telegram", "chat_id": 123, "chat_type": "private"}

        out = await orch.run(session, "do things", bot=fakebot, context=None, dest=dest)

        assert captured["max_tokens"] == 32768
        assert out == "## Итог\n\n- пункт 1"

    asyncio.run(_run())


def test_orchestrator_repo_grounded_final_qc_uses_runtime_fallback_after_compose_normalize_fallback(
    tmp_path,
    monkeypatch,
):
    async def _run():
        cfg = AppConfig(
            telegram=TelegramConfig(token="", whitelist_chat_ids=[]),
            tools={},
            defaults=DefaultsConfig(
                workdir=str(tmp_path),
                state_path=str(tmp_path / "state.json"),
                toolhelp_path=str(tmp_path / "toolhelp.json"),
                log_path=str(tmp_path / "bot.log"),
            ),
            mcp=MCPConfig(enabled=False),
            mcp_clients=[],
            presets=[],
            path=str(tmp_path / "config.yaml"),
        )

        def _template_provider(_session):
            return {
                "name": "Repo change spec",
                "required_sections": [
                    "Контекст текущей системы",
                    "Implementation handoff по компонентам и файлам",
                    "План тестирования и приемки",
                ],
                "qa_prompt": "QA-SPEC",
                "output_kind": "spec",
                "compose_mode": "template_first",
                "protected_spec_shell": {
                    "title": "Техническое задание",
                    "source_task_section": "Исходная задача",
                    "core_sections": [
                        "Контекст текущей системы",
                        "Implementation handoff по компонентам и файлам",
                        "План тестирования и приемки",
                    ],
                    "open_questions_section": "Открытые вопросы и валидационные шаги",
                },
                "repo_grounded_required": True,
            }

        orch = OrchestratorRunner(
            cfg,
            final_rework_enabled=False,
            template_provider=_template_provider,
        )
        compose_calls = {"count": 0}
        monkeypatch.setattr(orch, "_missing_required_repo_use_cli_step_ids", lambda *args, **kwargs: [])

        async def _fake_plan_steps(_cfg, _user_text, _ctx):
            return [PlanStep(id="step1", title="repo audit", instruction="audit", step_type="use_cli")]

        monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)

        async def _fake_execute_step(
            step,
            _session,
            _bot,
            _context,
            _dest,
            _orchestrator_context,
            *,
            current_user_text="",
            constraints=None,
        ):
            _ = step, current_user_text, constraints
            return ExecutorResponse(
                task_id="use_cli_repo_final_review",
                status="ok",
                summary="Финальный repo review подтверждает текущие формулировки.",
                outputs=[
                    {
                        "type": "repo_evidence",
                        "path": str(tmp_path / "app" / "services" / "session_transfer" / "service.py"),
                        "content_preview": "service.py",
                    }
                ],
                tool_calls=[{"tool": "fake"}],
                next_questions=[],
            )

        monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)
        monkeypatch.setattr(orch, "_maybe_update_memory", lambda *_a, **_k: asyncio.sleep(0))

        async def _fake_chat_completion(_cfg, _system, _user, response_format=None, **kwargs):
            if "Материалы (JSON):" in str(_user or ""):
                compose_calls["count"] += 1
                assert response_format == {"type": "json_object"}
                handler = kwargs.get("normalize_error_handler")
                assert callable(handler)
                broken = (
                    '{"final_text":"## Контекст текущей системы\\n\\n'
                    '- session transfer уже существует.\\n\\n'
                    '## Implementation handoff по компонентам и файлам\\n\\n'
                    '- Компонент/файл: app/services/session_transfer/service.py\\n'
                    '- Что меняется: добавить codex reader/writer seams.\\n'
                    '- Как проверить: .venv/bin/pytest -q tests/test_session_transfer.py\\n'
                    '- Тесты/команды: .venv/bin/pytest -q tests/test_session_transfer.py\\n\\n'
                    '## План тестирования и приемки\\n\\n'
                    '- .venv/bin/pytest -q tests/test_session_transfer.py'
                )
                return handler(broken, json.JSONDecodeError("Unterminated string", broken, 14))
            raise AssertionError("LLM-based final QC assessment should not run after compose normalize fallback")

        _fake_chat_completion._supports_strict_json_contract = True
        monkeypatch.setattr(orch._deps, "chat_completion", _fake_chat_completion)

        fakebot = _FakeBot()
        session = type(
            "S",
            (),
            {
                "id": "s1",
                "workdir": str(tmp_path),
                "project_root": str(tmp_path),
                "active_mode": "analyst",
                "analyst_intent_flags": {"clarification_is_blocking": False, "document_kind": "spec"},
            },
        )()
        dest = {"kind": "telegram", "chat_id": 123, "chat_type": "private"}

        out = await orch.run(
            session,
            "Подготовь ТЗ на перенос сессий.",
            bot=fakebot,
            context=None,
            dest=dest,
        )

        assert compose_calls["count"] == 1
        assert "## Контекст текущей системы" in out
        assert "## Implementation handoff по компонентам и файлам" in out
        assert ".venv/bin/pytest -q tests/test_session_transfer.py" in out

    asyncio.run(_run())


def test_orchestrator_compose_final_answer_keeps_full_content_preview_in_payload(tmp_path, monkeypatch):
    async def _run():
        cfg = AppConfig(
            telegram=TelegramConfig(token="", whitelist_chat_ids=[]),
            tools={},
            defaults=DefaultsConfig(
                workdir=str(tmp_path),
                state_path=str(tmp_path / "state.json"),
                toolhelp_path=str(tmp_path / "toolhelp.json"),
                log_path=str(tmp_path / "bot.log"),
            ),
            mcp=MCPConfig(enabled=False),
            mcp_clients=[],
            presets=[],
            path=str(tmp_path / "config.yaml"),
        )
        orch = OrchestratorRunner(cfg)
        long_preview = "P" * 1705
        captured = {"user": None}

        async def _fake_plan_steps(_cfg, _user_text, _ctx):
            return [PlanStep(id="step1", title="s1", instruction="do 1")]

        monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)

        async def _fake_execute_step(
            step,
            _session,
            _bot,
            _context,
            _dest,
            _orchestrator_context,
            *,
            current_user_text="",
            constraints=None,
        ):
            _ = current_user_text, constraints
            return ExecutorResponse(
                task_id=step.id,
                status="ok",
                summary=f"done {step.id}",
                outputs=[{"type": "text", "content": long_preview}],
                tool_calls=[{"tool": "fake"}],
                next_questions=[],
            )

        monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)
        monkeypatch.setattr(orch, "_maybe_update_memory", lambda *_a, **_k: asyncio.sleep(0))

        async def _fake_chat_completion(_cfg, _system, user):
            captured["user"] = user
            return "FINAL ANSWER"

        monkeypatch.setattr(orch._deps, "chat_completion", _fake_chat_completion)

        fakebot = _FakeBot()
        session = type("S", (), {"id": "s1"})
        dest = {"kind": "telegram", "chat_id": 123, "chat_type": "private"}

        out = await orch.run(session, "do things", bot=fakebot, context=None, dest=dest)

        assert out == "FINAL ANSWER"
        raw_user = str(captured["user"] or "")
        payload = json.loads(raw_user.split("Материалы (JSON):\n", 1)[1])
        assert payload["step_results"][0]["outputs"][0]["content_preview"] == long_preview
        assert "...(truncated)" not in raw_user

    asyncio.run(_run())


def test_orchestrator_compose_final_answer_repairs_missing_handoff_section_from_fallback(tmp_path, monkeypatch):
    async def _run():
        cfg = AppConfig(
            telegram=TelegramConfig(token="", whitelist_chat_ids=[]),
            tools={},
            defaults=DefaultsConfig(
                workdir=str(tmp_path),
                state_path=str(tmp_path / "state.json"),
                toolhelp_path=str(tmp_path / "toolhelp.json"),
                log_path=str(tmp_path / "bot.log"),
            ),
            mcp=MCPConfig(enabled=False),
            mcp_clients=[],
            presets=[],
            path=str(tmp_path / "config.yaml"),
        )

        def _template_provider(_session):
            return {
                "name": "Repo change spec",
                "required_sections": [
                    "Контекст текущей системы",
                    "Implementation handoff по компонентам и файлам",
                    "План тестирования и приемки",
                ],
                "qa_prompt": "QA-SPEC",
                "output_kind": "spec",
                "compose_mode": "template_first",
                "protected_spec_shell": {
                    "title": "Техническое задание",
                    "source_task_section": "Исходная задача",
                    "core_sections": [
                        "Контекст текущей системы",
                        "Implementation handoff по компонентам и файлам",
                        "План тестирования и приемки",
                    ],
                    "open_questions_section": "Открытые вопросы и валидационные шаги",
                },
                "repo_grounded_required": True,
            }

        orch = OrchestratorRunner(
            cfg,
            final_rework_enabled=False,
            template_provider=_template_provider,
        )
        compose_calls = {"count": 0}
        monkeypatch.setattr(orch, "_missing_required_repo_use_cli_step_ids", lambda *args, **kwargs: [])

        async def _fake_plan_steps(_cfg, _user_text, _ctx):
            return [PlanStep(id="step1", title="repo audit", instruction="audit", step_type="use_cli")]

        monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)

        async def _fake_execute_step(
            step,
            _session,
            _bot,
            _context,
            _dest,
            _orchestrator_context,
            *,
            current_user_text="",
            constraints=None,
        ):
            _ = current_user_text, constraints
            return ExecutorResponse(
                task_id=step.id,
                status="ok",
                summary="Обновить session.py и modes/sdk/runtime/cli_contracts.py для корректного structured completion.",
                outputs=[
                    {
                        "type": "repo_evidence",
                        "path": str(tmp_path / "session.py"),
                        "content_preview": "session.py",
                    },
                    {
                        "type": "repo_evidence",
                        "path": str(tmp_path / "modes" / "sdk" / "runtime" / "cli_contracts.py"),
                        "content_preview": "modes/sdk/runtime/cli_contracts.py",
                    },
                    {
                        "type": "text",
                        "content": (
                            "Проверка: .venv/bin/pytest -q "
                            "tests/test_session_resume_from_stderr.py tests/test_cli_contracts.py"
                        ),
                    },
                ],
                tool_calls=[{"tool": "fake"}],
                next_questions=[],
            )

        monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)
        monkeypatch.setattr(orch, "_maybe_update_memory", lambda *_a, **_k: asyncio.sleep(0))

        async def _fake_chat_completion(_cfg, _system, _user, response_format=None, **_kwargs):
            if "Материалы (JSON):" in str(_user or ""):
                compose_calls["count"] += 1
                assert response_format == {"type": "json_object"}
                return json.dumps(
                    {
                        "final_text": (
                            "## Контекст текущей системы\n\n"
                            "- Structured completion для codex нужно стабилизировать.\n\n"
                            "## План тестирования и приемки\n\n"
                            "- .venv/bin/pytest -q tests/test_session_resume_from_stderr.py tests/test_cli_contracts.py\n"
                        )
                    },
                    ensure_ascii=False,
                )
            return json.dumps(
                {
                    "needs_rework": False,
                    "issues": [],
                    "missing_sections": [],
                    "required_input_gaps": [],
                    "placeholder_gaps": [],
                    "implementation_handoff_gaps": [],
                    "spec_to_plan_gaps": [],
                    "weak_sections": [],
                    "missing_counts": [],
                    "traceability_gaps": [],
                    "codebase_mismatches": [],
                    "unsupported_assumptions": [],
                    "unverified_claims": [],
                    "evidence_gaps": [],
                    "config_contract_gaps": [],
                    "migration_gaps": [],
                    "doc_sync_gaps": [],
                    "test_gaps": [],
                    "external_reference_gaps": [],
                },
                ensure_ascii=False,
            )

        _fake_chat_completion._supports_strict_json_contract = True
        monkeypatch.setattr(orch._deps, "chat_completion", _fake_chat_completion)

        fakebot = _FakeBot()
        session = type(
            "S",
            (),
            {
                "id": "s1",
                "workdir": str(tmp_path),
                "project_root": str(tmp_path),
                "active_mode": "analyst",
                "analyst_intent_flags": {"clarification_is_blocking": False, "document_kind": "spec"},
            },
        )()
        dest = {"kind": "telegram", "chat_id": 123, "chat_type": "private"}

        out = await orch.run(
            session,
            "Подготовь ТЗ на стабилизацию финализации analyst.",
            bot=fakebot,
            context=None,
            dest=dest,
        )

        assert compose_calls["count"] == 1
        assert "## Implementation handoff по компонентам и файлам" in out
        assert "Компонент/файл: session.py" in out
        assert ".venv/bin/pytest -q tests/test_session_resume_from_stderr.py tests/test_cli_contracts.py" in out

    asyncio.run(_run())


def test_orchestrator_document_lint_repairs_unbalanced_fence_and_persists_report(tmp_path, monkeypatch):
    async def _run():
        cfg = AppConfig(
            telegram=TelegramConfig(token="", whitelist_chat_ids=[]),
            tools={},
            defaults=DefaultsConfig(
                workdir=str(tmp_path),
                state_path=str(tmp_path / "state.json"),
                toolhelp_path=str(tmp_path / "toolhelp.json"),
                log_path=str(tmp_path / "bot.log"),
            ),
            mcp=MCPConfig(enabled=False),
            mcp_clients=[],
            presets=[],
            path=str(tmp_path / "config.yaml"),
        )
        orch = OrchestratorRunner(cfg)

        async def _fake_plan_steps(_cfg, _user_text, _ctx):
            return [PlanStep(id="step1", title="s1", instruction="do 1")]

        monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)

        async def _fake_execute_step(
            step,
            _session,
            _bot,
            _context,
            _dest,
            _orchestrator_context,
            *,
            current_user_text="",
            constraints=None,
        ):
            _ = current_user_text, constraints
            return ExecutorResponse(
                task_id=step.id,
                status="ok",
                summary="done",
                outputs=[{"type": "text", "content": "out"}],
                tool_calls=[{"tool": "fake"}],
                next_questions=[],
            )

        monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)

        async def _noop_memory(*_args, **_kwargs):
            return None

        monkeypatch.setattr(orch, "_maybe_update_memory", _noop_memory)

        async def _fake_chat_completion(_cfg, _system, _user):
            return "## Doc\n\n```python\nprint('x')\n"

        monkeypatch.setattr(orch._deps, "chat_completion", _fake_chat_completion)

        fakebot = _FakeBot()
        session = type("S", (), {"id": "s1"})
        dest = {"kind": "telegram", "chat_id": 123, "chat_type": "private"}

        out = await orch.run(session, "do things", bot=fakebot, context=None, dest=dest)
        assert out.endswith("\n```\n") or out.endswith("\n```")
        lint_report_path = next(Path(tmp_path).rglob("s1_document_lint.md"))
        lint_report = lint_report_path.read_text(encoding="utf-8")
        assert "unbalanced_fenced_code_blocks" in lint_report
        assert "closed_unbalanced_fenced_code_blocks" in lint_report

    asyncio.run(_run())


def test_orchestrator_compose_final_answer_includes_fact_pack_and_spilled_artifact_refs(tmp_path, monkeypatch):
    async def _run():
        cfg = AppConfig(
            telegram=TelegramConfig(token="", whitelist_chat_ids=[]),
            tools={},
            defaults=DefaultsConfig(
                workdir=str(tmp_path),
                state_path=str(tmp_path / "state.json"),
                toolhelp_path=str(tmp_path / "toolhelp.json"),
                log_path=str(tmp_path / "bot.log"),
            ),
            mcp=MCPConfig(enabled=False),
            mcp_clients=[],
            presets=[],
            path=str(tmp_path / "config.yaml"),
        )
        orch = OrchestratorRunner(cfg)
        long_output = "Z" * 9000
        captured = {"user": None}

        async def _fake_plan_steps(_cfg, _user_text, _ctx):
            return [PlanStep(id="step1", title="s1", instruction="do 1")]

        monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)

        async def _fake_execute_step(
            step,
            _session,
            _bot,
            _context,
            _dest,
            _orchestrator_context,
            *,
            current_user_text="",
            constraints=None,
        ):
            _ = current_user_text, constraints
            return ExecutorResponse(
                task_id=step.id,
                status="ok",
                summary=f"done {step.id}",
                outputs=[{"type": "text", "content": long_output}],
                tool_calls=[{"tool": "fake"}],
                next_questions=[],
            )

        monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)
        monkeypatch.setattr(orch, "_maybe_update_memory", lambda *_a, **_k: asyncio.sleep(0))

        async def _fake_chat_completion(_cfg, _system, user):
            captured["user"] = user
            return "FINAL ANSWER"

        monkeypatch.setattr(orch._deps, "chat_completion", _fake_chat_completion)

        fakebot = _FakeBot()
        session = type("S", (), {"id": "s1"})
        dest = {"kind": "telegram", "chat_id": 123, "chat_type": "private"}

        out = await orch.run(session, "do things", bot=fakebot, context=None, dest=dest)

        assert out.startswith("FINAL ANSWER")
        assert "### Артефакты" in out
        raw_user = str(captured["user"] or "")
        payload = json.loads(raw_user.split("Материалы (JSON):\n", 1)[1])
        assert "Fact Pack" in payload["fact_pack_text"]
        assert payload["claim_ledger"]
        step_result = payload["step_results"][0]
        assert step_result["step_artifact"]
        text_output = step_result["outputs"][0]
        assert text_output["content_spilled"] is True
        assert text_output["content_len"] == len(long_output)
        assert text_output["content_preview"] == long_output[:2000]
        assert text_output["path"]

    asyncio.run(_run())


def test_orchestrator_compose_final_answer_uses_artifact_bundle_as_primary_source(tmp_path, monkeypatch):
    async def _run():
        cfg = AppConfig(
            telegram=TelegramConfig(token="", whitelist_chat_ids=[]),
            tools={},
            defaults=DefaultsConfig(
                workdir=str(tmp_path),
                state_path=str(tmp_path / "state.json"),
                toolhelp_path=str(tmp_path / "toolhelp.json"),
                log_path=str(tmp_path / "bot.log"),
            ),
            mcp=MCPConfig(enabled=False),
            mcp_clients=[],
            presets=[],
            path=str(tmp_path / "config.yaml"),
        )
        orch = OrchestratorRunner(cfg)
        captured = {"user": None}

        async def _fake_plan_steps(_cfg, _user_text, _ctx):
            return [PlanStep(id="step1", title="s1", instruction="do 1")]

        monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)

        async def _fake_execute_step(
            step,
            _session,
            _bot,
            _context,
            _dest,
            _orchestrator_context,
            *,
            current_user_text="",
            constraints=None,
        ):
            _ = current_user_text, constraints
            return ExecutorResponse(
                task_id=step.id,
                status="ok",
                summary="Подтвержден один ключевой факт",
                outputs=[{"type": "text", "content": "Файл views/header.blade.php содержит account dropdown"}],
                claims=[{"claim_id": "c1", "status": "confirmed", "text": "Header содержит account dropdown", "evidence": []}],
                tool_calls=[{"tool": "fake"}],
                next_questions=[],
            )

        monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)
        monkeypatch.setattr(orch, "_maybe_update_memory", lambda *_a, **_k: asyncio.sleep(0))

        async def _fake_chat_completion(_cfg, _system, user):
            captured["user"] = user
            return "FINAL ANSWER"

        monkeypatch.setattr(orch._deps, "chat_completion", _fake_chat_completion)

        fakebot = _FakeBot()
        session = type("S", (), {"id": "s1"})
        dest = {"kind": "telegram", "chat_id": 123, "chat_type": "private"}

        out = await orch.run(session, "Сделай анализ header", bot=fakebot, context=None, dest=dest)
        assert out == "FINAL ANSWER"

        raw_user = str(captured["user"] or "")
        payload = json.loads(raw_user.split("Материалы (JSON):\n", 1)[1])
        bundle = payload["artifact_bundle"]
        assert bundle["compose_mode"] == "artifacts_first"
        assert payload["user_query_path"]
        assert payload["fact_pack_path"]
        assert payload["claim_ledger_path"]
        assert payload["artifacts_index_path"]
        assert payload["user_query_path"] in bundle["primary_sources"]
        assert payload["fact_pack_path"] in bundle["primary_sources"]
        assert payload["claim_ledger_path"] in bundle["primary_sources"]
        assert payload["artifacts_index_path"] in bundle["primary_sources"]

    asyncio.run(_run())


def test_orchestrator_compose_final_answer_includes_repo_review_critical_findings(tmp_path, monkeypatch):
    async def _run():
        cfg = AppConfig(
            telegram=TelegramConfig(token="", whitelist_chat_ids=[]),
            tools={},
            defaults=DefaultsConfig(
                workdir=str(tmp_path),
                state_path=str(tmp_path / "state.json"),
                toolhelp_path=str(tmp_path / "toolhelp.json"),
                log_path=str(tmp_path / "bot.log"),
            ),
            mcp=MCPConfig(enabled=False),
            mcp_clients=[],
            presets=[],
            path=str(tmp_path / "config.yaml"),
        )

        def _template_provider(_session):
            return {
                "name": "Repo change spec",
                "required_sections": [
                    "Контекст текущей системы",
                    "Implementation handoff по компонентам и файлам",
                    "План тестирования и приемки",
                ],
                "qa_prompt": "QA-SPEC",
                "output_kind": "spec",
                "compose_mode": "template_first",
                "protected_spec_shell": {
                    "title": "Техническое задание",
                    "source_task_section": "Исходная задача",
                    "core_sections": [
                        "Контекст текущей системы",
                        "Implementation handoff по компонентам и файлам",
                        "План тестирования и приемки",
                    ],
                    "open_questions_section": "Открытые вопросы и валидационные шаги",
                },
                "repo_grounded_required": True,
            }

        orch = OrchestratorRunner(
            cfg,
            final_rework_enabled=False,
            template_provider=_template_provider,
        )
        captured = {"system": None, "payload": None}
        monkeypatch.setattr(orch, "_missing_required_repo_use_cli_step_ids", lambda *args, **kwargs: [])

        async def _fake_plan_steps(_cfg, _user_text, _ctx):
            return [
                PlanStep(id="synthesize_final_tz", title="synth", instruction="synth", step_type="use_cli"),
                PlanStep(
                    id="validate_tz_completeness",
                    title="validate",
                    instruction="validate",
                    step_type="use_cli",
                    depends_on=["synthesize_final_tz"],
                ),
                PlanStep(
                    id="use_cli_repo_final_review",
                    title="review",
                    instruction="review",
                    step_type="use_cli",
                    depends_on=["validate_tz_completeness"],
                ),
            ]

        monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)

        async def _fake_execute_step(
            step,
            _session,
            _bot,
            _context,
            _dest,
            _orchestrator_context,
            *,
            current_user_text="",
            constraints=None,
        ):
            _ = current_user_text, constraints
            if step.id == "synthesize_final_tz":
                return ExecutorResponse(
                    task_id=step.id,
                    status="ok",
                    summary="Черновик ТЗ собран.",
                    outputs=[{"type": "text", "content": "draft"}],
                    tool_calls=[{"tool": "fake"}],
                    next_questions=[],
                )
            if step.id == "validate_tz_completeness":
                return ExecutorResponse(
                    task_id=step.id,
                    status="ok",
                    summary="Нужно запретить запись resume_token в активный не-codex CLI.",
                    outputs=[
                        {
                            "type": "repo_review_correction",
                            "content": "Зафиксировать правило записи только в resume_tokens[\"codex\"].",
                        },
                        {
                            "type": "open_gap",
                            "content": "Локально подтвердить on-disk Codex session format перед writer_codex.",
                        },
                    ],
                    tool_calls=[{"tool": "fake"}],
                    next_questions=[],
                )
            return ExecutorResponse(
                task_id=step.id,
                status="ok",
                summary="Финальный review нашёл ещё один must-fix.",
                outputs=[
                    {
                        "type": "repo_review_correction",
                        "content": "Заменить bare pytest на .venv/bin/pytest -q в разделе проверки.",
                    },
                    {
                        "type": "repo_review_unverified_claim",
                        "content": "Не утверждать отдельный MiniApp switch flow без repo evidence.",
                    },
                ],
                tool_calls=[{"tool": "fake"}],
                next_questions=[],
            )

        monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)
        monkeypatch.setattr(orch, "_maybe_update_memory", lambda *_a, **_k: asyncio.sleep(0))

        async def _fake_chat_completion(_cfg, system, user, response_format=None, **_kwargs):
            if "Материалы (JSON):" in str(user or ""):
                captured["system"] = system
                captured["payload"] = json.loads(str(user).split("Материалы (JSON):\n", 1)[1])
                assert response_format == {"type": "json_object"}
                return json.dumps(
                    {
                        "final_text": (
                            "## Контекст текущей системы\n\n"
                            "- Reader/writer для codex ещё не реализованы.\n\n"
                            "## Implementation handoff по компонентам и файлам\n\n"
                            "- Компонент/файл: app/services/session_transfer/writer_codex.py\n"
                            "- Что меняется: писать token только в resume_tokens[\"codex\"].\n"
                            "- Как проверить: .venv/bin/pytest -q tests/test_session_transfer.py\n"
                            "- Тесты/команды: .venv/bin/pytest -q tests/test_session_transfer.py\n\n"
                            "## План тестирования и приемки\n\n"
                            "- Перед writer_codex отдельно подтвердить on-disk Codex session format.\n"
                        )
                    },
                    ensure_ascii=False,
                )
            return json.dumps(
                {
                    "needs_rework": False,
                    "issues": [],
                    "missing_sections": [],
                    "required_input_gaps": [],
                    "placeholder_gaps": [],
                    "implementation_handoff_gaps": [],
                    "spec_to_plan_gaps": [],
                    "weak_sections": [],
                    "missing_counts": [],
                    "traceability_gaps": [],
                    "codebase_mismatches": [],
                    "unsupported_assumptions": [],
                    "unverified_claims": [],
                    "evidence_gaps": [],
                    "config_contract_gaps": [],
                    "migration_gaps": [],
                    "doc_sync_gaps": [],
                    "test_gaps": [],
                    "external_reference_gaps": [],
                },
                ensure_ascii=False,
            )

        _fake_chat_completion._supports_strict_json_contract = True
        monkeypatch.setattr(orch._deps, "chat_completion", _fake_chat_completion)

        fakebot = _FakeBot()
        session = type(
            "S",
            (),
            {
                "id": "s1",
                "workdir": str(tmp_path),
                "project_root": str(tmp_path),
                "active_mode": "analyst",
                "analyst_intent_flags": {"clarification_is_blocking": False, "document_kind": "spec"},
            },
        )()
        dest = {"kind": "telegram", "chat_id": 123, "chat_type": "private"}

        out = await orch.run(
            session,
            "Подготовь ТЗ на перенос сессий между 4 CLI.",
            bot=fakebot,
            context=None,
            dest=dest,
        )

        assert "resume_tokens[\"codex\"]" in out
        assert captured["payload"] is not None
        critical_findings = captured["payload"]["critical_findings"]
        assert "Нужно запретить запись resume_token в активный не-codex CLI." in critical_findings
        assert "Зафиксировать правило записи только в resume_tokens[\"codex\"]." in critical_findings
        assert "Локально подтвердить on-disk Codex session format перед writer_codex." in critical_findings
        assert "Заменить bare pytest на .venv/bin/pytest -q в разделе проверки." in critical_findings
        assert "Не утверждать отдельный MiniApp switch flow без repo evidence." in critical_findings
        assert "CRITICAL FINDINGS ИЗ ФИНАЛЬНОЙ ПРОВЕРКИ" in str(captured["system"] or "")

    asyncio.run(_run())


def test_orchestrator_compose_final_answer_includes_required_input_closure_guidance(tmp_path, monkeypatch):
    async def _run():
        cfg = AppConfig(
            telegram=TelegramConfig(token="", whitelist_chat_ids=[]),
            tools={},
            defaults=DefaultsConfig(
                workdir=str(tmp_path),
                state_path=str(tmp_path / "state.json"),
                toolhelp_path=str(tmp_path / "toolhelp.json"),
                log_path=str(tmp_path / "bot.log"),
            ),
            mcp=MCPConfig(enabled=False),
            mcp_clients=[],
            presets=[],
            path=str(tmp_path / "config.yaml"),
        )

        def _template_provider(_session):
            return {
                "name": "Repo change spec",
                "required_sections": [
                    "Контекст текущей системы",
                    "Подтвержденные изменения по компонентам",
                    "Implementation handoff по компонентам и файлам",
                    "План тестирования и приемки",
                ],
                "qa_prompt": "QA-SPEC",
                "output_kind": "spec",
                "compose_mode": "template_first",
                "protected_spec_shell": {
                    "title": "Техническое задание",
                    "source_task_section": "Исходная задача",
                    "core_sections": [
                        "Контекст текущей системы",
                        "Подтвержденные изменения по компонентам",
                        "Implementation handoff по компонентам и файлам",
                        "План тестирования и приемки",
                    ],
                    "open_questions_section": "Открытые вопросы и валидационные шаги",
                },
                "repo_grounded_required": True,
                "required_inputs": [
                    "Какие компоненты/модули затронуты или предположительно затронуты",
                    "Требования к обратной совместимости",
                ],
            }

        orch = OrchestratorRunner(
            cfg,
            final_rework_enabled=False,
            template_provider=_template_provider,
        )
        captured = {"system": None}
        monkeypatch.setattr(orch, "_missing_required_repo_use_cli_step_ids", lambda *args, **kwargs: [])

        async def _fake_plan_steps(_cfg, _user_text, _ctx):
            return [PlanStep(id="synthesize_final_tz", title="synth", instruction="synth", step_type="use_cli")]

        monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)

        async def _fake_execute_step(
            step,
            _session,
            _bot,
            _context,
            _dest,
            _orchestrator_context,
            *,
            current_user_text="",
            constraints=None,
        ):
            _ = step, current_user_text, constraints
            return ExecutorResponse(
                task_id="synthesize_final_tz",
                status="ok",
                summary="Черновик ТЗ собран.",
                outputs=[{"type": "text", "content": "draft"}],
                tool_calls=[{"tool": "fake"}],
                next_questions=[],
            )

        monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)
        monkeypatch.setattr(orch, "_maybe_update_memory", lambda *_a, **_k: asyncio.sleep(0))

        async def _fake_chat_completion(_cfg, system, user, response_format=None, **_kwargs):
            if "Материалы (JSON):" in str(user or ""):
                captured["system"] = system
                assert response_format == {"type": "json_object"}
                return json.dumps(
                    {
                        "final_text": (
                            "## Контекст текущей системы\n\n"
                            "- Reader/writer для codex ещё не реализованы.\n\n"
                            "## Подтвержденные изменения по компонентам\n\n"
                            "- Точно затронутые: app/services/session_transfer/service.py\n"
                            "- Предположительно затронутые / требуют отдельной проверки: sessions/session_run_service.py\n\n"
                            "## Implementation handoff по компонентам и файлам\n\n"
                            "- Компонент/файл: app/services/session_transfer/service.py\n"
                            "- Что меняется: добавить reader_codex.\n"
                            "- Как проверить: .venv/bin/pytest -q tests/test_session_transfer.py\n"
                            "- Тесты/команды: .venv/bin/pytest -q tests/test_session_transfer.py\n\n"
                            "## План тестирования и приемки\n\n"
                            "- Не регрессировать текущие сценарии claude/gemini/qwen.\n"
                        )
                    },
                    ensure_ascii=False,
                )
            return json.dumps(
                {
                    "needs_rework": False,
                    "issues": [],
                    "missing_sections": [],
                    "required_input_gaps": [],
                    "placeholder_gaps": [],
                    "implementation_handoff_gaps": [],
                    "spec_to_plan_gaps": [],
                    "weak_sections": [],
                    "missing_counts": [],
                    "traceability_gaps": [],
                    "codebase_mismatches": [],
                    "unsupported_assumptions": [],
                    "unverified_claims": [],
                    "evidence_gaps": [],
                    "config_contract_gaps": [],
                    "migration_gaps": [],
                    "doc_sync_gaps": [],
                    "test_gaps": [],
                    "external_reference_gaps": [],
                },
                ensure_ascii=False,
            )

        _fake_chat_completion._supports_strict_json_contract = True
        monkeypatch.setattr(orch._deps, "chat_completion", _fake_chat_completion)

        fakebot = _FakeBot()
        session = type(
            "S",
            (),
            {
                "id": "s1",
                "workdir": str(tmp_path),
                "project_root": str(tmp_path),
                "active_mode": "analyst",
                "analyst_intent_flags": {"clarification_is_blocking": False, "document_kind": "spec"},
            },
        )()
        dest = {"kind": "telegram", "chat_id": 123, "chat_type": "private"}

        out = await orch.run(
            session,
            "Подготовь ТЗ на перенос сессий между 4 CLI.",
            bot=fakebot,
            context=None,
            dest=dest,
        )

        assert "Точно затронутые" in out
        system_text = str(captured["system"] or "")
        assert "ОБЯЗАТЕЛЬНО ЗАКРОЙ REQUIRED_INPUTS" in system_text
        assert "Какие компоненты/модули затронуты или предположительно затронуты" in system_text
        assert "Требования к обратной совместимости" in system_text
        assert "явно раздели `точно затронутые` и `предположительно затронутые / требуют отдельной проверки` зоны" in system_text
        assert "какие текущие сценарии обязаны не регрессировать" in system_text

    asyncio.run(_run())


def test_orchestrator_compose_final_answer_does_not_block_on_send_output(tmp_path, monkeypatch):
    async def _run():
        cfg = AppConfig(
            telegram=TelegramConfig(token="", whitelist_chat_ids=[]),
            tools={},
            defaults=DefaultsConfig(
                workdir=str(tmp_path),
                state_path=str(tmp_path / "state.json"),
                toolhelp_path=str(tmp_path / "toolhelp.json"),
                log_path=str(tmp_path / "bot.log"),
            ),
            mcp=MCPConfig(enabled=False),
            mcp_clients=[],
            presets=[],
            path=str(tmp_path / "config.yaml"),
        )
        orch = OrchestratorRunner(cfg)

        async def _fake_plan_steps(_cfg, _user_text, _ctx):
            return [PlanStep(id="step1", title="s1", instruction="do 1")]

        monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)

        async def _fake_execute_step(
            step,
            _session,
            _bot,
            _context,
            _dest,
            _orchestrator_context,
            *,
            current_user_text="",
            constraints=None,
        ):
            return ExecutorResponse(
                task_id=step.id,
                status="ok",
                summary=f"done {step.id}",
                outputs=[{"type": "text", "content": "out"}],
                tool_calls=[{"tool": "fake"}],
                next_questions=[],
            )

        monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)
        monkeypatch.setattr(orch, "_maybe_update_memory", lambda *_a, **_k: asyncio.sleep(0))

        async def _fake_chat_completion(*_a, **_k):
            return "FINAL"

        monkeypatch.setattr(orch._deps, "chat_completion", _fake_chat_completion)

        gate = asyncio.Event()

        class _Bot(_FakeBot):
            async def send_output(self, _session, _dest, output: str, _context, **kwargs):
                # Block until gate is opened; orchestrator.run must not await this.
                await gate.wait()
                return await super().send_output(_session, _dest, output, _context, **kwargs)

        fakebot = _Bot()
        session = type("S", (), {"id": "s1"})
        dest = {"kind": "telegram", "chat_id": 123, "chat_type": "private"}

        t0 = asyncio.get_running_loop().time()
        out = await orch.run(session, "do things", bot=fakebot, context=None, dest=dest)
        dt = asyncio.get_running_loop().time() - t0
        assert out == "FINAL"
        assert dt < 0.5
        # Let background send finish cleanly.
        gate.set()
        await asyncio.wait_for(fakebot.send_output_called.wait(), timeout=1.0)

    asyncio.run(_run())


def test_orchestrator_final_rework_fact_pack_keeps_full_summary_and_preview(tmp_path, monkeypatch):
    async def _run():
        cfg = AppConfig(
            telegram=TelegramConfig(token="", whitelist_chat_ids=[]),
            tools={},
            defaults=DefaultsConfig(
                workdir=str(tmp_path),
                state_path=str(tmp_path / "state.json"),
                toolhelp_path=str(tmp_path / "toolhelp.json"),
                log_path=str(tmp_path / "bot.log"),
            ),
            mcp=MCPConfig(enabled=False),
            mcp_clients=[],
            presets=[],
            path=str(tmp_path / "config.yaml"),
        )
        orch = OrchestratorRunner(
            cfg,
            final_rework_enabled=True,
            final_rework_passes=1,
        )
        long_summary = "S" * 1705
        long_preview = "P" * 1705

        async def _fake_plan_steps(_cfg, _user_text, _ctx):
            return [PlanStep(id="step1", title="s1", instruction="do 1")]

        monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)

        async def _fake_execute_step(
            step,
            _session,
            _bot,
            _context,
            _dest,
            _orchestrator_context,
            *,
            current_user_text="",
            constraints=None,
        ):
            _ = current_user_text, constraints
            return ExecutorResponse(
                task_id=step.id,
                status="ok",
                summary=long_summary,
                outputs=[{"type": "text", "content": long_preview}],
                tool_calls=[{"tool": "fake"}],
                next_questions=[],
            )

        monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)

        async def _noop_memory(*_args, **_kwargs):
            return None

        monkeypatch.setattr(orch, "_maybe_update_memory", _noop_memory)

        async def _fake_chat_completion(_cfg, _system, _user, response_format=None):
            if response_format is not None:
                return json.dumps(
                    {
                        "needs_rework": False,
                        "issues": [],
                        "missing_sections": [],
                        "weak_sections": [],
                        "missing_counts": [],
                        "traceability_gaps": [],
                        "codebase_mismatches": [],
                        "unsupported_assumptions": [],
                        "unverified_claims": [],
                        "evidence_gaps": [],
                        "config_contract_gaps": [],
                        "migration_gaps": [],
                        "doc_sync_gaps": [],
                        "test_gaps": [],
                    },
                    ensure_ascii=False,
                )
            return "FINAL ANSWER"

        monkeypatch.setattr(orch._deps, "chat_completion", _fake_chat_completion)

        fakebot = _FakeBot()
        session = type("S", (), {"id": "s1"})()
        dest = {"kind": "telegram", "chat_id": 123, "chat_type": "private"}

        out = await orch.run(session, "do things", bot=fakebot, context=None, dest=dest)

        assert out == "FINAL ANSWER"
        fact_pack_path = next(Path(tmp_path).rglob("s1_fact_pack.md"))
        fact_pack_text = fact_pack_path.read_text(encoding="utf-8")
        assert long_summary in fact_pack_text
        assert f"  - {long_preview}" in fact_pack_text
        assert "...(truncated)" not in fact_pack_text

    asyncio.run(_run())


def test_orchestrator_compose_final_answer_sends_artifacts(tmp_path, monkeypatch):
    async def _run():
        cfg = AppConfig(
            telegram=TelegramConfig(token="", whitelist_chat_ids=[]),
            tools={},
            defaults=DefaultsConfig(
                workdir=str(tmp_path),
                state_path=str(tmp_path / "state.json"),
                toolhelp_path=str(tmp_path / "toolhelp.json"),
                log_path=str(tmp_path / "bot.log"),
            ),
            mcp=MCPConfig(enabled=False),
            mcp_clients=[],
            presets=[],
            path=str(tmp_path / "config.yaml"),
        )
        orch = OrchestratorRunner(cfg)

        artifact_path = tmp_path / "a.txt"
        artifact_path.write_text("hello", encoding="utf-8")

        async def _fake_plan_steps(_cfg, _user_text, _ctx):
            return [PlanStep(id="step1", title="s1", instruction="do 1")]

        monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)

        async def _fake_execute_step(
            step,
            _session,
            _bot,
            _context,
            _dest,
            _orchestrator_context,
            *,
            current_user_text="",
            constraints=None,
        ):
            return ExecutorResponse(
                task_id=step.id,
                status="ok",
                summary=f"done {step.id}",
                outputs=[
                    {"type": "file", "path": str(artifact_path), "name": "a.txt"},
                    {"type": "text", "content": "out"},
                ],
                tool_calls=[{"tool": "fake"}],
                next_questions=[],
            )

        monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)

        async def _noop_memory(*_args, **_kwargs):
            return None

        monkeypatch.setattr(orch, "_maybe_update_memory", _noop_memory)

        async def _fake_chat_completion(_cfg, _system, _user):
            return "FINAL"

        monkeypatch.setattr(orch._deps, "chat_completion", _fake_chat_completion)

        fakebot = _FakeBot()
        session = type("S", (), {"id": "s1"})
        dest = {"kind": "telegram", "chat_id": 123, "chat_type": "private"}

        await orch.run(session, "do things", bot=fakebot, context=None, dest=dest)
        await asyncio.wait_for(fakebot.doc_called.wait(), timeout=1.0)
        assert fakebot.sent_docs
        assert any(str(artifact_path) == p for p in fakebot.sent_docs)

    asyncio.run(_run())


def test_orchestrator_compose_final_answer_delivers_analyst_result_via_send_output_without_artifacts(tmp_path, monkeypatch):
    async def _run():
        cfg = AppConfig(
            telegram=TelegramConfig(token="", whitelist_chat_ids=[]),
            tools={},
            defaults=DefaultsConfig(
                workdir=str(tmp_path),
                state_path=str(tmp_path / "state.json"),
                toolhelp_path=str(tmp_path / "toolhelp.json"),
                log_path=str(tmp_path / "bot.log"),
            ),
            mcp=MCPConfig(enabled=False),
            mcp_clients=[],
            presets=[],
            path=str(tmp_path / "config.yaml"),
        )
        orch = OrchestratorRunner(cfg)

        async def _fake_plan_steps(_cfg, _user_text, _ctx):
            return [PlanStep(id="step1", title="s1", instruction="do 1")]

        monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)

        artifact_path = tmp_path / "artifact.txt"
        artifact_path.write_text("artifact", encoding="utf-8")

        async def _fake_execute_step(
            step,
            _session,
            _bot,
            _context,
            _dest,
            _orchestrator_context,
            *,
            current_user_text="",
            constraints=None,
        ):
            _ = step, current_user_text, constraints
            return ExecutorResponse(
                task_id="step1",
                status="ok",
                summary="done",
                outputs=[
                    {"type": "file", "path": str(artifact_path), "name": "artifact.txt"},
                    {"type": "text", "content": "FINAL ANSWER\n\n## ТЗ\n\nНужен только текст."},
                ],
                tool_calls=[{"tool": "fake"}],
                next_questions=[],
            )

        monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)
        monkeypatch.setattr(orch, "_maybe_update_memory", lambda *_a, **_k: asyncio.sleep(0))

        async def _fake_chat_completion(*_args, **_kwargs):
            return "FINAL ANSWER\n\n## ТЗ\n\nНужен только текст."

        monkeypatch.setattr(orch._deps, "chat_completion", _fake_chat_completion)

        fakebot = _FakeBot()
        session = type("S", (), {"id": "s-analyst", "active_mode": "analyst"})()
        dest = {"kind": "telegram", "chat_id": 123, "chat_type": "private"}

        out = await orch.run(session, "do things", bot=fakebot, context=None, dest=dest)

        assert "### Артефакты" not in out
        await asyncio.wait_for(fakebot.send_output_called.wait(), timeout=1.0)
        sent_output, send_kwargs = fakebot.sent_outputs[0]
        assert "Нужен только текст." in sent_output
        assert send_kwargs.get("force_html") is False
        assert fakebot.doc_called.is_set() is False
        assert any(e[0] == "msg" and "Готово" in e[2] for e in fakebot.events)

    asyncio.run(_run())


def test_orchestrator_compose_final_answer_detects_analyst_delivery_via_runtime_markers(tmp_path, monkeypatch):
    async def _run():
        cfg = AppConfig(
            telegram=TelegramConfig(token="", whitelist_chat_ids=[]),
            tools={},
            defaults=DefaultsConfig(
                workdir=str(tmp_path),
                state_path=str(tmp_path / "state.json"),
                toolhelp_path=str(tmp_path / "toolhelp.json"),
                log_path=str(tmp_path / "bot.log"),
            ),
            mcp=MCPConfig(enabled=False),
            mcp_clients=[],
            presets=[],
            path=str(tmp_path / "config.yaml"),
        )
        captured = {"system": None}
        orch = OrchestratorRunner(
            cfg,
            template_provider=lambda _session: {
                "compose_mode": "template_first",
                "output_kind": "spec",
                "required_sections": ["Контекст", "Изменения", "Приемка"],
                "system_prompt_addition": "Не перечисляй все поверхности проекта.",
            },
        )

        async def _fake_plan_steps(_cfg, _user_text, _ctx):
            return [PlanStep(id="step1", title="s1", instruction="do 1")]

        monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)

        artifact_path = tmp_path / "artifact.txt"
        artifact_path.write_text("artifact", encoding="utf-8")

        async def _fake_execute_step(
            step,
            _session,
            _bot,
            _context,
            _dest,
            _orchestrator_context,
            *,
            current_user_text="",
            constraints=None,
        ):
            _ = step, current_user_text, constraints
            return ExecutorResponse(
                task_id="step1",
                status="ok",
                summary="done",
                outputs=[
                    {"type": "file", "path": str(artifact_path), "name": "artifact.txt"},
                    {"type": "text", "content": "FINAL ANSWER\n\n## ТЗ\n\nТолько итоговый документ."},
                ],
                tool_calls=[{"tool": "fake"}],
                next_questions=[],
            )

        monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)
        monkeypatch.setattr(orch, "_maybe_update_memory", lambda *_a, **_k: asyncio.sleep(0))

        async def _fake_chat_completion(_cfg, system, _user):
            captured["system"] = system
            return "FINAL ANSWER\n\n## ТЗ\n\nТолько итоговый документ."

        monkeypatch.setattr(orch._deps, "chat_completion", _fake_chat_completion)

        fakebot = _FakeBot()
        session = type(
            "S",
            (),
            {
                "id": "s-analyst-markers",
                "modes": type("Modes", (), {"active_mode": None})(),
                "executor_profile": "analyst",
                "cli": type("Cli", (), {"cli_work_type": "analytics"})(),
            },
        )()
        dest = {"kind": "telegram", "chat_id": 123, "chat_type": "private"}

        out = await orch.run(session, "do things", bot=fakebot, context=None, dest=dest)

        assert "### Артефакты" not in out
        await asyncio.wait_for(fakebot.send_output_called.wait(), timeout=1.0)
        sent_output, send_kwargs = fakebot.sent_outputs[0]
        assert "Только итоговый документ." in sent_output
        assert send_kwargs.get("force_html") is False
        assert fakebot.doc_called.is_set() is False
        assert "ФОКУС ПО SCOPE:" in str(captured["system"] or "")
        assert "Не перечисляй все поверхности проекта." in str(captured["system"] or "")

    asyncio.run(_run())


def test_orchestrator_compose_final_answer_detects_analyst_delivery_via_execution_context(tmp_path, monkeypatch):
    async def _run():
        cfg = AppConfig(
            telegram=TelegramConfig(token="", whitelist_chat_ids=[]),
            tools={},
            defaults=DefaultsConfig(
                workdir=str(tmp_path),
                state_path=str(tmp_path / "state.json"),
                toolhelp_path=str(tmp_path / "toolhelp.json"),
                log_path=str(tmp_path / "bot.log"),
            ),
            mcp=MCPConfig(enabled=False),
            mcp_clients=[],
            presets=[],
            path=str(tmp_path / "config.yaml"),
        )
        captured = {"system": None}
        orch = OrchestratorRunner(
            cfg,
            template_provider=lambda _session: {
                "compose_mode": "template_first",
                "output_kind": "spec",
                "required_sections": ["Контекст", "Изменения", "Приемка"],
                "system_prompt_addition": "Не перечисляй все поверхности проекта.",
            },
        )

        async def _fake_plan_steps(_cfg, _user_text, _ctx):
            return [PlanStep(id="step1", title="s1", instruction="do 1")]

        monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)

        artifact_path = tmp_path / "artifact.txt"
        artifact_path.write_text("artifact", encoding="utf-8")

        state_path = tmp_path / "analyst-run-state.json"
        state_path.write_text(
            json.dumps(
                {
                    "mode_context": {
                        "execution_context": {
                            "dest_kind": "telegram",
                            "chat_id": 123,
                            "active_flow": "spec",
                        }
                    }
                }
            ),
            encoding="utf-8",
        )

        async def _fake_execute_step(
            step,
            _session,
            _bot,
            _context,
            _dest,
            _orchestrator_context,
            *,
            current_user_text="",
            constraints=None,
        ):
            _ = step, current_user_text, constraints
            return ExecutorResponse(
                task_id="step1",
                status="ok",
                summary="done",
                outputs=[
                    {"type": "file", "path": str(artifact_path), "name": "artifact.txt"},
                    {"type": "text", "content": "FINAL ANSWER\n\n## ТЗ\n\nТолько итоговый документ."},
                ],
                tool_calls=[{"tool": "fake"}],
                next_questions=[],
            )

        monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)
        monkeypatch.setattr(orch, "_maybe_update_memory", lambda *_a, **_k: asyncio.sleep(0))

        async def _fake_chat_completion(_cfg, system, _user):
            captured["system"] = system
            return "FINAL ANSWER\n\n## ТЗ\n\nТолько итоговый документ."

        monkeypatch.setattr(orch._deps, "chat_completion", _fake_chat_completion)

        fakebot = _FakeBot()
        session = type(
            "S",
            (),
            {
                "id": "s-analyst-execution-context",
                "modes": type("Modes", (), {"active_mode": None})(),
                "analyst_run_artifact_handle": SimpleNamespace(state_path=str(state_path)),
            },
        )()
        dest = {"kind": "telegram", "chat_id": 123, "chat_type": "private"}

        out = await orch.run(session, "do things", bot=fakebot, context=None, dest=dest)

        assert "### Артефакты" not in out
        await asyncio.wait_for(fakebot.send_output_called.wait(), timeout=1.0)
        sent_output, send_kwargs = fakebot.sent_outputs[0]
        assert "Только итоговый документ." in sent_output
        assert send_kwargs.get("force_html") is False
        assert fakebot.doc_called.is_set() is False
        assert "ФОКУС ПО SCOPE:" in str(captured["system"] or "")
        assert "Не перечисляй все поверхности проекта." in str(captured["system"] or "")

    asyncio.run(_run())


def test_orchestrator_skips_memory_update_for_strict_analyst_runtime(tmp_path, monkeypatch):
    async def _run():
        cfg = AppConfig(
            telegram=TelegramConfig(token="", whitelist_chat_ids=[]),
            tools={},
            defaults=DefaultsConfig(
                workdir=str(tmp_path),
                state_path=str(tmp_path / "state.json"),
                toolhelp_path=str(tmp_path / "toolhelp.json"),
                log_path=str(tmp_path / "bot.log"),
            ),
            mcp=MCPConfig(enabled=False),
            mcp_clients=[],
            presets=[],
            path=str(tmp_path / "config.yaml"),
        )
        orch = OrchestratorRunner(cfg)

        async def _fake_plan_steps(_cfg, _user_text, _ctx):
            return [PlanStep(id="step1", title="s1", instruction="do 1")]

        monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)

        async def _fake_execute_step(
            step,
            _session,
            _bot,
            _context,
            _dest,
            _orchestrator_context,
            *,
            current_user_text="",
            constraints=None,
        ):
            _ = step, current_user_text, constraints
            return ExecutorResponse(
                task_id="step1",
                status="ok",
                summary="done",
                outputs=[{"type": "text", "content": "FINAL ANSWER"}],
                tool_calls=[{"tool": "fake"}],
                next_questions=[],
            )

        monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)

        async def _fail_memory(*_args, **_kwargs):
            raise AssertionError("_maybe_update_memory should not run for strict analyst runtime")

        monkeypatch.setattr(orch, "_maybe_update_memory", _fail_memory)

        async def _fake_chat_completion(_cfg, _system, _user):
            return "FINAL ANSWER"

        monkeypatch.setattr(orch._deps, "chat_completion", _fake_chat_completion)

        fakebot = _FakeBot()
        session = type(
            "S",
            (),
            {
                "id": "s-analyst-no-memory",
                "modes": type("Modes", (), {"active_mode": "analyst"})(),
                "executor_profile": "analyst",
            },
        )()
        dest = {"kind": "telegram", "chat_id": 123, "chat_type": "private"}

        out = await orch.run(session, "do things", bot=fakebot, context=None, dest=dest)

        assert out == "FINAL ANSWER"
        await asyncio.wait_for(fakebot.send_output_called.wait(), timeout=1.0)

    asyncio.run(_run())


def test_orchestrator_compose_final_answer_background_reports_use_notification_queue(tmp_path, monkeypatch):
    async def _run():
        cfg = AppConfig(
            telegram=TelegramConfig(token="", whitelist_chat_ids=[]),
            tools={},
            defaults=DefaultsConfig(
                workdir=str(tmp_path),
                state_path=str(tmp_path / "state.json"),
                toolhelp_path=str(tmp_path / "toolhelp.json"),
                log_path=str(tmp_path / "bot.log"),
            ),
            mcp=MCPConfig(enabled=False),
            mcp_clients=[],
            presets=[],
            path=str(tmp_path / "config.yaml"),
        )
        orch = OrchestratorRunner(cfg)

        artifact_path = tmp_path / "a.txt"
        artifact_path.write_text("hello", encoding="utf-8")

        async def _fake_plan_steps(_cfg, _user_text, _ctx):
            return [PlanStep(id="step1", title="s1", instruction="do 1")]

        monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)

        async def _fake_execute_step(
            step,
            _session,
            _bot,
            _context,
            _dest,
            _orchestrator_context,
            *,
            current_user_text="",
            constraints=None,
        ):
            return ExecutorResponse(
                task_id=step.id,
                status="ok",
                summary=f"done {step.id}",
                outputs=[
                    {"type": "file", "path": str(artifact_path), "name": "a.txt"},
                    {"type": "text", "content": "out"},
                ],
                tool_calls=[{"tool": "fake"}],
                next_questions=[],
            )

        monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)
        monkeypatch.setattr(orch, "_maybe_update_memory", lambda *_a, **_k: asyncio.sleep(0))

        async def _fake_chat_completion(*_a, **_k):
            return "FINAL"

        monkeypatch.setattr(orch._deps, "chat_completion", _fake_chat_completion)

        events = []
        send_output_started = asyncio.Event()
        release_send_output = asyncio.Event()

        class _Bot(_FakeBot):
            def __init__(self):
                super().__init__()
                self.notification_queue_service = NotificationQueueService(min_interval_sec=0.0)

            async def send_output(self, _session, _dest, output: str, _context, **kwargs):
                self.events.append(("send_output", output, kwargs))
                self.sent_outputs.append((output, kwargs))
                self.send_output_called.set()
                send_output_started.set()
                await release_send_output.wait()

            async def _send_document(self, _context, *, chat_id: int, document, **_kwargs):
                self.events.append(("doc", chat_id, getattr(document, "name", "")))
                self.sent_docs.append(getattr(document, "name", ""))
                self.doc_called.set()
                return True

        fakebot = _Bot()
        await fakebot.notification_queue_service.start()
        session = type(
            "S",
            (),
            {
                "id": "s1",
                "conversation_scope": ConversationScope(chat_id=-100777000111, message_thread_id=202),
            },
        )()
        dest = {
            "kind": "telegram",
            "chat_id": -100777000111,
            "message_thread_id": 202,
            "chat_type": "supergroup",
        }

        await orch.run(session, "do things", bot=fakebot, context=None, dest=dest)
        await asyncio.wait_for(send_output_started.wait(), timeout=1.0)

        async def _other() -> str:
            events.append(("other", "queued"))
            return "other"

        other_task = asyncio.create_task(
            fakebot.notification_queue_service.enqueue(
                session.conversation_scope,
                operation="other",
                factory=_other,
            )
        )
        await asyncio.sleep(0)
        assert other_task.done() is False

        release_send_output.set()
        await asyncio.wait_for(fakebot.doc_called.wait(), timeout=1.0)
        assert await other_task == "other"
        await fakebot.notification_queue_service.shutdown()

        assert fakebot.events[0][0] == "msg"
        assert fakebot.events[1][0] == "send_output"
        assert fakebot.events[2][0] == "doc"
        assert events == [("other", "queued")]

    asyncio.run(_run())


def test_orchestrator_compose_final_answer_tracks_background_send_as_session_task(tmp_path, monkeypatch):
    async def _run():
        cfg = AppConfig(
            telegram=TelegramConfig(token="", whitelist_chat_ids=[]),
            tools={},
            defaults=DefaultsConfig(
                workdir=str(tmp_path),
                state_path=str(tmp_path / "state.json"),
                toolhelp_path=str(tmp_path / "toolhelp.json"),
                log_path=str(tmp_path / "bot.log"),
            ),
            mcp=MCPConfig(enabled=False),
            mcp_clients=[],
            presets=[],
            path=str(tmp_path / "config.yaml"),
        )
        orch = OrchestratorRunner(cfg)

        async def _fake_plan_steps(_cfg, _user_text, _ctx):
            return [PlanStep(id="step1", title="s1", instruction="do 1")]

        monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)

        async def _fake_execute_step(
            step,
            _session,
            _bot,
            _context,
            _dest,
            _orchestrator_context,
            *,
            current_user_text="",
            constraints=None,
        ):
            _ = constraints
            return ExecutorResponse(
                task_id=step.id,
                status="ok",
                summary=f"done {step.id}",
                outputs=[{"type": "text", "content": "out"}],
                tool_calls=[{"tool": "fake"}],
                next_questions=[],
            )

        monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)
        monkeypatch.setattr(orch, "_maybe_update_memory", lambda *_a, **_k: asyncio.sleep(0))
        monkeypatch.setattr(orch._deps, "chat_completion", lambda *_a, **_k: asyncio.sleep(0, result="FINAL"))

        gate = asyncio.Event()
        mode_tasks = ModeTaskService()

        class _Bot(_FakeBot):
            def __init__(self):
                super().__init__()
                self.mode_tasks = mode_tasks

            async def send_output(self, _session, _dest, output: str, _context, **kwargs):
                _ = output, kwargs
                self.send_output_called.set()
                await gate.wait()

        fakebot = _Bot()
        session = type(
            "S",
            (),
            {
                "id": "s1",
                "conversation_scope": ConversationScope(chat_id=-100777000111, message_thread_id=202),
            },
        )()
        dest = {"kind": "telegram", "chat_id": -100777000111, "message_thread_id": 202, "chat_type": "supergroup"}

        out = await orch.run(session, "do things", bot=fakebot, context=None, dest=dest)
        assert out == "FINAL"
        await asyncio.wait_for(fakebot.send_output_called.wait(), timeout=1.0)
        assert mode_tasks.list_session(session_uid=session_runtime_uid(session)) == ["orchestrator_final_send"]

        cancelled = await mode_tasks.cancel_session(session_uid=session_runtime_uid(session), timeout_s=0.5)
        assert cancelled == 1
        await asyncio.sleep(0)
        assert mode_tasks.list_session(session_uid=session_runtime_uid(session)) == []

    asyncio.run(_run())


def test_orchestrator_compose_final_answer_waits_for_analyst_send_output(tmp_path, monkeypatch):
    async def _run():
        cfg = AppConfig(
            telegram=TelegramConfig(token="", whitelist_chat_ids=[]),
            tools={},
            defaults=DefaultsConfig(
                workdir=str(tmp_path),
                state_path=str(tmp_path / "state.json"),
                toolhelp_path=str(tmp_path / "toolhelp.json"),
                log_path=str(tmp_path / "bot.log"),
            ),
            mcp=MCPConfig(enabled=False),
            mcp_clients=[],
            presets=[],
            path=str(tmp_path / "config.yaml"),
        )
        orch = OrchestratorRunner(cfg)

        async def _fake_plan_steps(_cfg, _user_text, _ctx):
            return [PlanStep(id="step1", title="s1", instruction="do 1")]

        monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)

        async def _fake_execute_step(
            step,
            _session,
            _bot,
            _context,
            _dest,
            _orchestrator_context,
            *,
            current_user_text="",
            constraints=None,
        ):
            _ = step, current_user_text, constraints
            return ExecutorResponse(
                task_id="step1",
                status="ok",
                summary="done",
                outputs=[{"type": "text", "content": "FINAL ANSWER\n\n## ТЗ\n\nНужен только текст."}],
                tool_calls=[{"tool": "fake"}],
                next_questions=[],
            )

        monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)
        monkeypatch.setattr(orch, "_maybe_update_memory", lambda *_a, **_k: asyncio.sleep(0))
        monkeypatch.setattr(orch._deps, "chat_completion", lambda *_a, **_k: asyncio.sleep(0, result="FINAL"))

        gate = asyncio.Event()
        mode_tasks = ModeTaskService()

        class _Bot(_FakeBot):
            def __init__(self):
                super().__init__()
                self.mode_tasks = mode_tasks

            async def send_output(self, _session, _dest, output: str, _context, **kwargs):
                self.events.append(("send_output", output, kwargs))
                self.sent_outputs.append((output, kwargs))
                self.send_output_called.set()
                await gate.wait()

        fakebot = _Bot()
        session = type("S", (), {"id": "s-analyst", "active_mode": "analyst"})()
        dest = {"kind": "telegram", "chat_id": 123, "chat_type": "private"}

        run_task = asyncio.create_task(orch.run(session, "do things", bot=fakebot, context=None, dest=dest))
        await asyncio.wait_for(fakebot.send_output_called.wait(), timeout=1.0)
        await asyncio.sleep(0)
        assert run_task.done() is False
        assert mode_tasks.list_session(session_uid=session_runtime_uid(session)) == []

        gate.set()
        out = await asyncio.wait_for(run_task, timeout=1.0)
        assert out == "FINAL"

    asyncio.run(_run())


def test_orchestrator_template_first_compose_uses_required_sections_contract(tmp_path, monkeypatch):
    async def _run():
        cfg = AppConfig(
            telegram=TelegramConfig(token="", whitelist_chat_ids=[]),
            tools={},
            defaults=DefaultsConfig(
                workdir=str(tmp_path),
                state_path=str(tmp_path / "state.json"),
                toolhelp_path=str(tmp_path / "toolhelp.json"),
                log_path=str(tmp_path / "bot.log"),
            ),
            mcp=MCPConfig(enabled=False),
            mcp_clients=[],
            presets=[],
            path=str(tmp_path / "config.yaml"),
        )

        def _template_provider(_session):
            return {
                "compose_mode": "template_first",
                "output_kind": "spec",
                "required_sections": ["Контекст", "Функциональные требования", "Критерии приемки"],
                "system_prompt_addition": "Не перечисляй все поверхности проекта.",
            }

        orch = OrchestratorRunner(
            cfg,
            final_rework_enabled=True,
            final_rework_passes=0,
            template_provider=_template_provider,
        )

        async def _fake_plan_steps(_cfg, _user_text, _ctx):
            return [PlanStep(id="step1", title="s1", instruction="do 1")]

        monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)

        async def _fake_execute_step(
            step,
            _session,
            _bot,
            _context,
            _dest,
            _orchestrator_context,
            *,
            current_user_text="",
            constraints=None,
        ):
            return ExecutorResponse(
                task_id=step.id,
                status="ok",
                summary=f"done {step.id}",
                outputs=[{"type": "text", "content": "out"}],
                tool_calls=[{"tool": "fake"}],
                next_questions=[],
            )

        monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)

        async def _noop_memory(*_args, **_kwargs):
            return None

        monkeypatch.setattr(orch, "_maybe_update_memory", _noop_memory)
        captured = {"system": None}

        async def _fake_chat_completion(_cfg, system, _user):
            captured["system"] = system
            assert "СТРОГИЙ КОНТРАКТ ДОКУМЕНТА" in system
            assert "- Контекст" in system
            assert "- Функциональные требования" in system
            assert "- Критерии приемки" in system
            assert "ФОКУС ПО SCOPE:" in system
            assert "Не перечисляй все поверхности проекта." in system
            assert "ЖЁСТКИЙ КОНТРАКТ (обязательные разделы, в этом порядке)" not in system
            return (
                "## Контекст\n"
                "Контекст заполнен.\n\n"
                "## Функциональные требования\n"
                "Требования заполнены.\n\n"
                "## Критерии приемки\n"
                "Критерии заполнены."
            )

        monkeypatch.setattr(orch._deps, "chat_completion", _fake_chat_completion)

        fakebot = _FakeBot()
        session = type("S", (), {"id": "s1"})
        dest = {"kind": "telegram", "chat_id": 123, "chat_type": "private"}

        out = await orch.run(session, "do things", bot=fakebot, context=None, dest=dest)

        assert captured["system"] is not None
        assert out.startswith("## Контекст")
        await asyncio.wait_for(fakebot.send_output_called.wait(), timeout=1.0)
        sent = fakebot.sent_outputs[0]
        assert sent[0] == out
        assert "## Контекст" in sent[0]
        assert "## Функциональные требования" in sent[0]
        assert "## Критерии приемки" in sent[0]

    asyncio.run(_run())


def test_orchestrator_template_first_compose_prefers_session_cli(tmp_path, monkeypatch):
    async def _run():
        cfg = AppConfig(
            telegram=TelegramConfig(token="", whitelist_chat_ids=[]),
            tools={},
            defaults=DefaultsConfig(
                workdir=str(tmp_path),
                state_path=str(tmp_path / "state.json"),
                toolhelp_path=str(tmp_path / "toolhelp.json"),
                log_path=str(tmp_path / "bot.log"),
            ),
            mcp=MCPConfig(enabled=False),
            mcp_clients=[],
            presets=[],
            path=str(tmp_path / "config.yaml"),
        )

        def _template_provider(_session):
            return {
                "compose_mode": "template_first",
                "output_kind": "spec",
                "required_sections": ["Контекст", "Функциональные требования", "Критерии приемки"],
            }

        orch = OrchestratorRunner(
            cfg,
            final_rework_enabled=True,
            final_rework_passes=0,
            template_provider=_template_provider,
        )

        async def _fake_plan_steps(_cfg, _user_text, _ctx):
            return [PlanStep(id="step1", title="s1", instruction="do 1")]

        monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)

        async def _fake_execute_step(
            step,
            _session,
            _bot,
            _context,
            _dest,
            _orchestrator_context,
            *,
            current_user_text="",
            constraints=None,
        ):
            _ = current_user_text, constraints
            return ExecutorResponse(
                task_id=step.id,
                status="ok",
                summary=f"done {step.id}",
                outputs=[{"type": "text", "content": "out"}],
                tool_calls=[{"tool": "fake"}],
                next_questions=[],
            )

        monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)
        monkeypatch.setattr(orch, "_maybe_update_memory", lambda *_a, **_k: asyncio.sleep(0))

        captured = {"prompt": None, "chat_called": 0}

        async def _fake_chat_completion(*_args, **_kwargs):
            captured["chat_called"] += 1
            raise AssertionError("compose_final_answer should use session.run_prompt for template-first compose")

        monkeypatch.setattr(orch._deps, "chat_completion", _fake_chat_completion)

        async def _session_run_prompt(prompt: str) -> str:
            captured["prompt"] = prompt
            assert f"CLI_RESPONSE_FORMAT: {CLIResponseFormat.CLAIM_BUNDLE_JSON}" in prompt
            assert "СТРОГИЙ КОНТРАКТ ДОКУМЕНТА" in prompt
            return json.dumps(
                {
                    "final_text": (
                        "## Контекст\nКонтекст заполнен.\n\n"
                        "## Функциональные требования\nТребования заполнены.\n\n"
                        "## Критерии приемки\nКритерии заполнены."
                    ),
                    "claims": [],
                    "evidence": [],
                    "open_gaps": [],
                },
                ensure_ascii=False,
            )

        fakebot = _FakeBot()
        session = type(
            "S",
            (),
            {
                "id": "s-cli-compose",
                "run_prompt": lambda self, prompt: _session_run_prompt(prompt),
            },
        )()
        dest = {"kind": "telegram", "chat_id": 123, "chat_type": "private"}

        out = await orch.run(session, "do things", bot=fakebot, context=None, dest=dest)

        assert captured["prompt"] is not None
        assert captured["chat_called"] == 0
        assert out.startswith("## Контекст")
        await asyncio.wait_for(fakebot.send_output_called.wait(), timeout=1.0)
        assert "## Критерии приемки" in fakebot.sent_outputs[0][0]

    asyncio.run(_run())


def test_orchestrator_template_first_compose_spills_large_cli_prompt_to_artifact(tmp_path, monkeypatch):
    async def _run():
        cfg = AppConfig(
            telegram=TelegramConfig(token="", whitelist_chat_ids=[]),
            tools={},
            defaults=DefaultsConfig(
                workdir=str(tmp_path),
                state_path=str(tmp_path / "state.json"),
                toolhelp_path=str(tmp_path / "toolhelp.json"),
                log_path=str(tmp_path / "bot.log"),
            ),
            mcp=MCPConfig(enabled=False),
            mcp_clients=[],
            presets=[],
            path=str(tmp_path / "config.yaml"),
        )

        def _template_provider(_session):
            return {
                "compose_mode": "template_first",
                "output_kind": "spec",
                "required_sections": ["Контекст", "Функциональные требования", "Критерии приемки"],
            }

        orch = OrchestratorRunner(
            cfg,
            final_rework_enabled=True,
            final_rework_passes=0,
            template_provider=_template_provider,
        )
        monkeypatch.setattr(orchestrator_runner_module, "_CLI_PROMPT_ARTIFACT_THRESHOLD_BYTES", 512)

        big_payload = "X" * 100000

        async def _fake_plan_steps(_cfg, _user_text, _ctx):
            return [PlanStep(id="step1", title="s1", instruction="do 1")]

        async def _fake_execute_step(
            step,
            _session,
            _bot,
            _context,
            _dest,
            _orchestrator_context,
            *,
            current_user_text="",
            constraints=None,
        ):
            _ = current_user_text, constraints
            return ExecutorResponse(
                task_id=step.id,
                status="ok",
                summary="done",
                outputs=[{"type": "text", "content": big_payload}],
                tool_calls=[{"tool": "fake"}],
                next_questions=[],
            )

        monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)
        monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)
        monkeypatch.setattr(orch, "_maybe_update_memory", lambda *_a, **_k: asyncio.sleep(0))
        monkeypatch.setattr(orch, "_missing_required_repo_use_cli_step_ids", lambda *args, **kwargs: [])

        async def _fake_chat_completion(*_args, **_kwargs):
            raise AssertionError("compose_final_answer should use session.run_prompt for template-first compose")

        monkeypatch.setattr(orch._deps, "chat_completion", _fake_chat_completion)

        captured = {"prompt": None, "file_text": None, "file_path": None}

        async def _session_run_prompt(prompt: str) -> str:
            captured["prompt"] = prompt
            first_line = prompt.splitlines()[0]
            assert first_line.startswith("Сначала полностью прочитай файл @")
            assert f"CLI_RESPONSE_FORMAT: {CLIResponseFormat.CLAIM_BUNDLE_JSON}" in prompt
            assert "СТРОГИЙ КОНТРАКТ ДОКУМЕНТА" not in prompt
            assert big_payload not in prompt
            file_ref = first_line.split("@", 1)[1].split(" ", 1)[0]
            prompt_candidates = list(tmp_path.rglob(Path(file_ref).name))
            assert prompt_candidates
            prompt_file = prompt_candidates[0]
            captured["file_path"] = prompt_file
            captured["file_text"] = prompt_file.read_text(encoding="utf-8")
            assert "СТРОГИЙ КОНТРАКТ ДОКУМЕНТА" in captured["file_text"]
            assert "Материалы (JSON):" in captured["file_text"]
            return json.dumps(
                {
                    "final_text": (
                        "## Контекст\nКонтекст заполнен.\n\n"
                        "## Функциональные требования\nТребования заполнены.\n\n"
                        "## Критерии приемки\nКритерии заполнены."
                    ),
                    "claims": [],
                    "evidence": [],
                    "open_gaps": [],
                },
                ensure_ascii=False,
            )

        fakebot = _FakeBot()
        session = type(
            "S",
            (),
            {
                "id": "s-cli-compose-large",
                "run_prompt": lambda self, prompt: _session_run_prompt(prompt),
            },
        )()
        dest = {"kind": "telegram", "chat_id": 123, "chat_type": "private"}

        out = await orch.run(session, "do things", bot=fakebot, context=None, dest=dest)

        assert captured["prompt"] is not None
        assert captured["file_path"] is not None
        assert captured["file_path"].is_file()
        assert out.startswith("## Контекст")
        await asyncio.wait_for(fakebot.send_output_called.wait(), timeout=1.0)

    asyncio.run(_run())


def test_orchestrator_template_first_compose_persists_staged_final_candidate(tmp_path, monkeypatch):
    async def _run():
        cfg = AppConfig(
            telegram=TelegramConfig(token="", whitelist_chat_ids=[]),
            tools={},
            defaults=DefaultsConfig(
                workdir=str(tmp_path),
                state_path=str(tmp_path / "state.json"),
                toolhelp_path=str(tmp_path / "toolhelp.json"),
                log_path=str(tmp_path / "bot.log"),
            ),
            mcp=MCPConfig(enabled=False),
            mcp_clients=[],
            presets=[],
            path=str(tmp_path / "config.yaml"),
        )

        def _template_provider(_session):
            return {
                "compose_mode": "template_first",
                "output_kind": "spec",
                "required_sections": ["Контекст", "Функциональные требования", "Критерии приемки"],
            }

        orch = OrchestratorRunner(
            cfg,
            final_rework_enabled=True,
            final_rework_passes=0,
            template_provider=_template_provider,
        )

        async def _fake_plan_steps(_cfg, _user_text, _ctx):
            return [PlanStep(id="step1", title="s1", instruction="do 1")]

        async def _fake_execute_step(
            step,
            _session,
            _bot,
            _context,
            _dest,
            _orchestrator_context,
            *,
            current_user_text="",
            constraints=None,
        ):
            _ = current_user_text, constraints
            return ExecutorResponse(
                task_id=step.id,
                status="ok",
                summary="done",
                outputs=[{"type": "text", "content": "repo-grounded summary"}],
                tool_calls=[{"tool": "fake"}],
                next_questions=[],
            )

        monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)
        monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)
        monkeypatch.setattr(orch, "_maybe_update_memory", lambda *_a, **_k: asyncio.sleep(0))
        monkeypatch.setattr(orch, "_missing_required_repo_use_cli_step_ids", lambda *args, **kwargs: [])

        async def _fake_chat_completion(*_args, **_kwargs):
            raise AssertionError("compose_final_answer should use session.run_prompt for template-first compose")

        monkeypatch.setattr(orch._deps, "chat_completion", _fake_chat_completion)

        final_markdown = (
            "## Контекст\n"
            "Нужно поддержать перенос сессий.\n\n"
            "## Функциональные требования\n"
            "- Добавить чтение и запись.\n\n"
            "## Критерии приемки\n"
            "- Реализован полный цикл.\n"
        )

        async def _session_run_prompt(_prompt: str) -> str:
            return json.dumps(
                {
                    "final_text": final_markdown,
                    "claims": [],
                    "evidence": [],
                    "open_gaps": [],
                },
                ensure_ascii=False,
            )

        staged_path = tmp_path / "output" / "final.staged.md"
        session = SimpleNamespace(
            id="s1",
            executor_profile="analyst",
            run_prompt=_session_run_prompt,
            analyst_runtime_final_candidate_path=str(staged_path),
        )
        fakebot = _FakeBot()
        dest = {"kind": "telegram", "chat_id": 123, "chat_type": "private"}

        out = await orch.run(session, "do things", bot=fakebot, context=None, dest=dest)

        assert out.startswith("## Контекст")
        assert staged_path.exists()
        assert staged_path.read_text(encoding="utf-8") == out + "\n"
        await asyncio.wait_for(fakebot.send_output_called.wait(), timeout=1.0)
        assert fakebot.sent_outputs[0][0] == out

    asyncio.run(_run())


def test_orchestrator_required_sections_default_to_template_first_contract(tmp_path, monkeypatch):
    async def _run():
        cfg = AppConfig(
            telegram=TelegramConfig(token="", whitelist_chat_ids=[]),
            tools={},
            defaults=DefaultsConfig(
                workdir=str(tmp_path),
                state_path=str(tmp_path / "state.json"),
                toolhelp_path=str(tmp_path / "toolhelp.json"),
                log_path=str(tmp_path / "bot.log"),
            ),
            mcp=MCPConfig(enabled=False),
            mcp_clients=[],
            presets=[],
            path=str(tmp_path / "config.yaml"),
        )

        def _template_provider(_session):
            return {
                "required_sections": ["Контекст", "Вывод"],
                "system_prompt_addition": "Не раздувай общий анализ в enterprise-спеку.",
            }

        orch = OrchestratorRunner(cfg, final_rework_enabled=True, template_provider=_template_provider)

        async def _fake_plan_steps(_cfg, _user_text, _ctx):
            return [PlanStep(id="step1", title="s1", instruction="do 1")]

        monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)

        async def _fake_execute_step(
            step,
            _session,
            _bot,
            _context,
            _dest,
            _orchestrator_context,
            *,
            current_user_text="",
            constraints=None,
        ):
            _ = step, current_user_text, constraints
            return ExecutorResponse(
                task_id="step1",
                status="ok",
                summary="done",
                outputs=[{"type": "text", "content": "analysis evidence"}],
                tool_calls=[{"tool": "fake"}],
                next_questions=[],
            )

        monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)
        monkeypatch.setattr(orch, "_maybe_update_memory", lambda *_a, **_k: asyncio.sleep(0))

        async def _fake_chat_completion(_cfg, system, _user):
            assert "СТРОГИЙ КОНТРАКТ ДОКУМЕНТА" in system
            assert "- Контекст" in system
            assert "- Вывод" in system
            assert "Не раздувай общий анализ в enterprise-спеку." in system
            assert "ЖЁСТКИЙ КОНТРАКТ (обязательные разделы, в этом порядке)" not in system
            return "## Контекст\nФакты.\n\n## Вывод\nРекомендация."

        monkeypatch.setattr(orch._deps, "chat_completion", _fake_chat_completion)

        fakebot = _FakeBot()
        session = type("S", (), {"id": "s-required-sections", "analyst_template_id": "default"})()
        dest = {"kind": "telegram", "chat_id": 123, "chat_type": "private"}

        out = await orch.run(session, "do things", bot=fakebot, context=None, dest=dest)

        assert out.startswith("## Контекст")
        await asyncio.wait_for(fakebot.send_output_called.wait(), timeout=1.0)
        assert "## Вывод" in fakebot.sent_outputs[0][0]

    asyncio.run(_run())


def test_orchestrator_legacy_final_answer_opt_out_preserves_generic_contract(tmp_path, monkeypatch):
    async def _run():
        cfg = AppConfig(
            telegram=TelegramConfig(token="", whitelist_chat_ids=[]),
            tools={},
            defaults=DefaultsConfig(
                workdir=str(tmp_path),
                state_path=str(tmp_path / "state.json"),
                toolhelp_path=str(tmp_path / "toolhelp.json"),
                log_path=str(tmp_path / "bot.log"),
            ),
            mcp=MCPConfig(enabled=False),
            mcp_clients=[],
            presets=[],
            path=str(tmp_path / "config.yaml"),
        )

        def _template_provider(_session):
            return {
                "compose_mode": "legacy_final_answer",
                "required_sections": ["Контекст", "Вывод"],
            }

        orch = OrchestratorRunner(cfg, final_rework_enabled=True, template_provider=_template_provider)

        async def _fake_plan_steps(_cfg, _user_text, _ctx):
            return [PlanStep(id="step1", title="s1", instruction="do 1")]

        monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)

        async def _fake_execute_step(
            step,
            _session,
            _bot,
            _context,
            _dest,
            _orchestrator_context,
            *,
            current_user_text="",
            constraints=None,
        ):
            _ = step, current_user_text, constraints
            return ExecutorResponse(
                task_id="step1",
                status="ok",
                summary="done",
                outputs=[{"type": "text", "content": "analysis evidence"}],
                tool_calls=[{"tool": "fake"}],
                next_questions=[],
            )

        monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)
        monkeypatch.setattr(orch, "_maybe_update_memory", lambda *_a, **_k: asyncio.sleep(0))

        async def _fake_chat_completion(_cfg, system, _user):
            assert "ЖЁСТКИЙ КОНТРАКТ (обязательные разделы, в этом порядке)" in system
            assert "СТРОГИЙ КОНТРАКТ ДОКУМЕНТА" not in system
            return "## Результат\nГотово.\n\n## Детали\n- факт"

        monkeypatch.setattr(orch._deps, "chat_completion", _fake_chat_completion)

        fakebot = _FakeBot()
        session = type("S", (), {"id": "s-legacy-final-answer", "analyst_template_id": "default"})()
        dest = {"kind": "telegram", "chat_id": 123, "chat_type": "private"}

        out = await orch.run(session, "do things", bot=fakebot, context=None, dest=dest)

        assert out.startswith("## Результат")
        await asyncio.wait_for(fakebot.send_output_called.wait(), timeout=1.0)
        assert "## Детали" in fakebot.sent_outputs[0][0]

    asyncio.run(_run())
