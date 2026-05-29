import asyncio

from config import AppConfig, DefaultsConfig, MCPConfig, TelegramConfig
from modes.sdk.runtime import planner


def test_planner_generates_specific_ask_user_via_llm(tmp_path, monkeypatch):
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

        # Force clarification path deterministically.
        monkeypatch.setattr(planner, "needs_clarification", lambda *_args, **_kwargs: True)

        calls = {"n": 0}

        async def _fake_chat_completion(_cfg, system, user, response_format=None):
            calls["n"] += 1
            # 1) main plan call: returns steps without ask_user
            if calls["n"] == 1:
                return (
                    '{'
                    '"steps": ['
                    '{"id":"step1","title":"Сделать работу","instruction":"do","step_type":"task",'
                    '"depends_on":[],"parallel_group":null,"parallelizable":false,'
                    '"parallelizable_reason":null,"ask_question":null,"ask_options":null}'
                    ']'
                    '}'
                )
            # 2) clarification call: returns specific question/options
            return """{
              "ask_question": "Какой формат вывода вам нужен?",
              "ask_options": ["Краткий список", "Подробный отчёт", "Только команды"]
            }"""

        monkeypatch.setattr(planner, "chat_completion", _fake_chat_completion)

        steps = await planner.plan_steps(cfg, "сделай X", "контекст")
        assert calls["n"] == 2
        assert steps[0].step_type == "ask_user"
        assert steps[0].ask_question == "Какой формат вывода вам нужен?"
        assert steps[0].ask_options[:2] == ["Краткий список", "Подробный отчёт"]

    asyncio.run(_run())


def test_planner_repairs_multi_aspect_or_multiple_ask_user_steps(tmp_path, monkeypatch):
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

        monkeypatch.setattr(planner, "needs_clarification", lambda *_args, **_kwargs: False)
        calls = {"n": 0}

        async def _fake_chat_completion(_cfg, system, user, response_format=None):
            del system, user, response_format
            calls["n"] += 1
            if calls["n"] == 1:
                return """{
                  "steps": [
                    {
                      "id":"ask1",
                      "title":"Уточнение",
                      "instruction":"ask",
                      "step_type":"ask_user",
                      "depends_on":[],
                      "parallel_group":"g1",
                      "parallelizable":true,
                      "parallelizable_reason":"safe",
                      "ask_question":"Уточните следующие аспекты:\\n1. Бизнес-цель\\n2. Сроки\\n3. Ограничения",
                      "ask_options":["a","b","c","d"]
                    },
                    {
                      "id":"ask2",
                      "title":"Еще уточнение",
                      "instruction":"ask2",
                      "step_type":"ask_user",
                      "depends_on":[],
                      "parallel_group":"g1",
                      "parallelizable":true,
                      "parallelizable_reason":"safe",
                      "ask_question":"Еще один вопрос",
                      "ask_options":["x","y"]
                    },
                    {
                      "id":"step2",
                      "title":"Сделать работу",
                      "instruction":"do",
                      "step_type":"task",
                      "depends_on":["ask2"],
                      "parallel_group":null,
                      "parallelizable":false,
                      "parallelizable_reason":null,
                      "ask_question":null,
                      "ask_options":null
                    }
                  ]
                }"""
            return """{
              "ask_question": "Какой формат результата нужен в первую очередь?",
              "ask_options": ["Краткий результат", "Подробное ТЗ", "Свой вариант"]
            }"""

        monkeypatch.setattr(planner, "chat_completion", _fake_chat_completion)

        steps = await planner.plan_steps(cfg, "сделай X", "контекст")
        ask_steps = [step for step in steps if step.step_type == "ask_user"]
        assert len(ask_steps) == 1
        assert ask_steps[0].id == "ask1"
        assert ask_steps[0].ask_question == "Какой формат результата нужен в первую очередь?"
        assert ask_steps[0].ask_options[:2] == ["Краткий результат", "Подробное ТЗ"]
        assert ask_steps[0].parallelizable is False
        assert ask_steps[0].parallel_group is None
        assert next(step for step in steps if step.id == "step2").depends_on == ["ask1"]

    asyncio.run(_run())


def test_planner_retries_rebuilt_ask_user_until_options_are_short_enough(tmp_path, monkeypatch):
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

        monkeypatch.setattr(planner, "needs_clarification", lambda *_args, **_kwargs: False)
        calls = {"n": 0}

        async def _fake_chat_completion(_cfg, system, user, response_format=None):
            del system, user, response_format
            calls["n"] += 1
            if calls["n"] == 1:
                return """{
                  "steps": [
                    {
                      "id":"ask1",
                      "title":"Уточнение",
                      "instruction":"ask",
                      "step_type":"ask_user",
                      "depends_on":[],
                      "parallel_group":null,
                      "parallelizable":false,
                      "parallelizable_reason":null,
                      "ask_question":"Какая платформа нужна?",
                      "ask_options":[
                        "Очень длинный вариант ответа, который точно не пройдет валидацию по ограничению длины вариантов",
                        "mobile"
                      ]
                    }
                  ]
                }"""
            if calls["n"] == 2:
                return """{
                  "ask_question": "Какая платформа нужна в первой версии?",
                  "ask_options": [
                    "Очень длинный вариант ответа, который снова превышает допустимый лимит длины для кнопки",
                    "Только mobile"
                  ]
                }"""
            return """{
              "ask_question": "Какая платформа нужна в первой версии?",
              "ask_options": ["Только web", "Только mobile", "Обе платформы"]
            }"""

        monkeypatch.setattr(planner, "chat_completion", _fake_chat_completion)

        steps = await planner.plan_steps(cfg, "Сделай ТЗ", "контекст")
        assert calls["n"] == 3
        ask_steps = [step for step in steps if step.step_type == "ask_user"]
        assert len(ask_steps) == 1
        assert ask_steps[0].ask_question == "Какая платформа нужна в первой версии?"
        assert ask_steps[0].ask_options == ["Только web", "Только mobile", "Обе платформы"]

    asyncio.run(_run())


def test_planner_adds_ask_user_from_analyst_intent_flags(tmp_path, monkeypatch):
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

        monkeypatch.setattr(planner, "needs_clarification", lambda *_args, **_kwargs: False)
        calls = {"n": 0}

        async def _fake_chat_completion(_cfg, _system, user, response_format=None):
            del response_format
            calls["n"] += 1
            if calls["n"] == 1:
                return (
                    '{'
                    '"steps": ['
                    '{"id":"step1","title":"Сделать работу","instruction":"do","step_type":"task",'
                    '"depends_on":[],"parallel_group":null,"parallelizable":false,'
                    '"parallelizable_reason":null,"ask_question":null,"ask_options":null}'
                    ']'
                    '}'
                )
            assert "Приоритетная тема уточнения" in user
            return """{
              "ask_question": "Какой результат нужен по мобильной платформе?",
              "ask_options": ["Только web", "Только mobile", "Оба варианта"]
            }"""

        monkeypatch.setattr(planner, "chat_completion", _fake_chat_completion)

        steps = await planner.plan_steps(
            cfg,
            "Сделай ТЗ",
            (
                "analyst_intent_flags:\n"
                '{"needs_clarification": true, "clarification_is_blocking": true, '
                '"clarification_topic": "Нужно уточнить платформу"}'
            ),
        )
        assert calls["n"] == 2
        assert steps[0].step_type == "ask_user"
        assert steps[0].ask_question == "Какой результат нужен по мобильной платформе?"
        assert steps[0].ask_options[:2] == ["Только web", "Только mobile"]
        assert next(step for step in steps if step.id == "step1").depends_on == ["ask_user_1"]

    asyncio.run(_run())


def test_planner_does_not_add_ask_user_from_required_inputs_when_flag_is_false(tmp_path, monkeypatch):
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

        monkeypatch.setattr(planner, "needs_clarification", lambda *_args, **_kwargs: False)
        calls = {"n": 0}

        async def _fake_chat_completion(_cfg, _system, user, response_format=None):
            del response_format
            calls["n"] += 1
            del user
            return (
                '{'
                '"steps": ['
                '{"id":"step1","title":"Сделать работу","instruction":"do","step_type":"task",'
                '"depends_on":[],"parallel_group":null,"parallelizable":false,'
                '"parallelizable_reason":null,"ask_question":null,"ask_options":null}'
                ']'
                '}'
            )

        monkeypatch.setattr(planner, "chat_completion", _fake_chat_completion)

        steps = await planner.plan_steps(
            cfg,
            "Сделай ТЗ",
            (
                "analyst_intent_flags:\n"
                '{"needs_clarification": false, "clarification_is_blocking": false, '
                '"template_id": "change_spec", '
                '"required_inputs": ["Платформа"]}'
            ),
        )
        assert calls["n"] == 1
        assert [step.id for step in steps] == ["step1"]
        assert steps[0].step_type == "task"

    asyncio.run(_run())


def test_planner_analyst_fallback_keeps_repo_grounded_steps_after_invalid_plan(tmp_path, monkeypatch):
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

        monkeypatch.setattr(planner, "needs_clarification", lambda *_args, **_kwargs: False)

        async def _fake_chat_completion(_cfg, _system, _user, response_format=None):
            del response_format
            return '{"steps": ["invalid"]}'

        monkeypatch.setattr(planner, "chat_completion", _fake_chat_completion)

        steps = await planner.plan_steps(
            cfg,
            "Подготовь ТЗ по Codex session transfer",
            (
                "executor_profile=analyst\n"
                "project_root=/tmp/project\n"
                "analyst_intent_flags:\n"
                '{"document_kind":"spec","needs_clarification":false,'
                '"requires_codebase_grounding":true,"requires_repo_audit":true,'
                '"requires_final_repo_review":true}'
            ),
        )
        step_ids = [step.id for step in steps]
        assert step_ids == ["use_cli_repo_audit", "step1", "use_cli_repo_final_review"]
        assert steps[1].title == "Подготовить repo-grounded ТЗ"
        assert "repo-grounded evidence" in steps[1].instruction

    asyncio.run(_run())


def test_planner_uses_deterministic_analyst_plan_for_repo_grounded_integration_spec(tmp_path, monkeypatch):
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

        monkeypatch.setattr(planner, "needs_clarification", lambda *_args, **_kwargs: False)

        async def _unexpected_chat_completion(*_args, **_kwargs):
            raise AssertionError("deterministic analyst plan should not call chat_completion")

        monkeypatch.setattr(planner, "chat_completion", _unexpected_chat_completion)

        steps = await planner.plan_steps(
            cfg,
            "Подготовь ТЗ и используй внешний референс https://example.com/codedash",
            (
                "executor_profile=analyst\n"
                f"project_root={tmp_path}\n"
                f"workdir={tmp_path}\n"
                "analyst_intent_flags:\n"
                '{"document_kind":"spec","template_id":"integration_change_spec",'
                '"needs_clarification":false,"requires_codebase_grounding":true,'
                '"requires_repo_audit":true,"requires_final_repo_review":true}'
            ),
        )

        step_ids = [step.id for step in steps]
        assert step_ids == [
            "use_cli_repo_audit",
            "analyze_external_reference",
            "synthesize_final_tz",
            "validate_tz_completeness",
            "use_cli_repo_final_review",
        ]
        synth_step = next(step for step in steps if step.id == "synthesize_final_tz")
        validate_step = next(step for step in steps if step.id == "validate_tz_completeness")
        final_review_step = next(step for step in steps if step.id == "use_cli_repo_final_review")
        assert synth_step.step_type == "use_cli"
        assert synth_step.depends_on == ["use_cli_repo_audit", "analyze_external_reference"]
        assert validate_step.step_type == "use_cli"
        assert validate_step.depends_on == ["synthesize_final_tz"]
        assert "https://example.com/codedash" in next(
            step for step in steps if step.id == "analyze_external_reference"
        ).instruction
        assert final_review_step.depends_on == ["use_cli_repo_audit"]

    asyncio.run(_run())


def test_planner_forces_analyst_ask_user_to_block_even_when_flag_is_false(tmp_path, monkeypatch):
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

        monkeypatch.setattr(planner, "needs_clarification", lambda *_args, **_kwargs: False)
        monkeypatch.setattr(planner, "ask_step_needs_rebuild", lambda *_args, **_kwargs: False)

        async def _fake_chat_completion(_cfg, _system, _user, response_format=None):
            del response_format
            return """{
              "steps": [
                {
                  "id":"step1",
                  "title":"Сделать работу",
                  "instruction":"do",
                  "step_type":"task",
                  "depends_on":[],
                  "parallel_group":null,
                  "parallelizable":false,
                  "parallelizable_reason":null,
                  "ask_question":null,
                  "ask_options":null
                },
                {
                  "id":"ask_later",
                  "title":"Уточнение",
                  "instruction":"ask",
                  "step_type":"ask_user",
                  "depends_on":[],
                  "parallel_group":null,
                  "parallelizable":false,
                  "parallelizable_reason":null,
                  "ask_question":"Какой вариант нужен?",
                  "ask_options":["A","B"]
                }
              ]
            }"""

        monkeypatch.setattr(planner, "chat_completion", _fake_chat_completion)

        steps = await planner.plan_steps(
            cfg,
            "Сделай ТЗ",
            (
                "analyst_intent_flags:\n"
                '{"needs_clarification": true, "clarification_is_blocking": false, '
                '"clarification_topic": "Нужно уточнить платформу"}'
            ),
        )

        assert steps[0].id == "ask_later"
        assert steps[0].step_type == "ask_user"
        assert next(step for step in steps if step.id == "step1").depends_on == ["ask_later"]

    asyncio.run(_run())


def test_planner_reuses_intent_clarification_contract_without_extra_llm_call(tmp_path, monkeypatch):
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

        monkeypatch.setattr(planner, "needs_clarification", lambda *_args, **_kwargs: False)
        calls = {"n": 0}

        async def _fake_chat_completion(_cfg, _system, _user, response_format=None):
            del response_format
            calls["n"] += 1
            if "Seed question" in _user:
                return '{"ask_question": "Какая платформа в приоритете?", "ask_options": ["web", "mobile"]}'
            return (
                '{'
                '"steps": ['
                '{"id":"step1","title":"Сделать работу","instruction":"do","step_type":"task",'
                '"depends_on":[],"parallel_group":null,"parallelizable":false,'
                '"parallelizable_reason":null,"ask_question":null,"ask_options":null}'
                ']'
                '}'
            )

        monkeypatch.setattr(planner, "chat_completion", _fake_chat_completion)

        steps = await planner.plan_steps(
            cfg,
            "Сделай ТЗ",
            (
                "analyst_intent_flags:\n"
                '{"needs_clarification": true, "clarification_is_blocking": true, '
                '"clarification_topic": "Нужно уточнить платформу", '
                '"clarification_question": "Какая платформа в приоритете?", '
                '"clarification_options": ["web", "mobile"], '
                '"template_id": "change_spec", '
                '"required_inputs": ["Платформа", "Ограничения"]}'
            ),
        )
        assert calls["n"] == 2
        assert steps[0].step_type == "ask_user"
        assert steps[0].ask_question == "Какая платформа в приоритете?"
        assert steps[0].ask_options == ["web", "mobile"]

    asyncio.run(_run())


def test_planner_does_not_force_second_clarification_after_answer(tmp_path, monkeypatch):
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

        monkeypatch.setattr(planner, "needs_clarification", lambda *_args, **_kwargs: False)

        async def _fake_chat_completion(_cfg, _system, _user, response_format=None):
            del response_format
            return (
                '{'
                '"steps": ['
                '{"id":"step1","title":"Сделать работу","instruction":"do","step_type":"task",'
                '"depends_on":[],"parallel_group":null,"parallelizable":false,'
                '"parallelizable_reason":null,"ask_question":null,"ask_options":null}'
                ']'
                '}'
            )

        monkeypatch.setattr(planner, "chat_completion", _fake_chat_completion)

        steps = await planner.plan_steps(
            cfg,
            "Сделай ТЗ\nОтвет пользователя: Только web",
            (
                "analyst_intent_flags:\n"
                '{"needs_clarification": true, "clarification_is_blocking": true, '
                '"clarification_topic": "Нужно уточнить платформу", '
                '"clarification_question": "Какая платформа в приоритете?", '
                '"clarification_options": ["web", "mobile"]}'
            ),
        )
        assert [step.id for step in steps] == ["step1"]

    asyncio.run(_run())


def test_planner_does_not_treat_control_answer_as_resolved_clarification(tmp_path, monkeypatch):
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

        monkeypatch.setattr(planner, "needs_clarification", lambda *_args, **_kwargs: False)
        calls = {"n": 0}

        async def _fake_chat_completion(_cfg, _system, _user, response_format=None):
            del response_format
            calls["n"] += 1
            if "Seed question" in _user:
                return '{"ask_question": "Какая платформа в приоритете?", "ask_options": ["web", "mobile"]}'
            return (
                '{'
                '"steps": ['
                '{"id":"step1","title":"Сделать работу","instruction":"do","step_type":"task",'
                '"depends_on":[],"parallel_group":null,"parallelizable":false,'
                '"parallelizable_reason":null,"ask_question":null,"ask_options":null}'
                ']'
                '}'
            )

        monkeypatch.setattr(planner, "chat_completion", _fake_chat_completion)

        steps = await planner.plan_steps(
            cfg,
            "Сделай ТЗ\nОтвет пользователя: Продолжить с предположениями",
            (
                "analyst_intent_flags:\n"
                '{"needs_clarification": true, "clarification_is_blocking": true, '
                '"clarification_topic": "Нужно уточнить платформу", '
                '"clarification_question": "Какая платформа в приоритете?", '
                '"clarification_options": ["web", "mobile"]}'
            ),
        )
        assert calls["n"] == 2
        assert steps[0].step_type == "ask_user"
        assert steps[0].ask_question == "Какая платформа в приоритете?"

    asyncio.run(_run())


def test_planner_clarification_prompt_receives_required_inputs(tmp_path, monkeypatch):
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

        monkeypatch.setattr(planner, "needs_clarification", lambda *_args, **_kwargs: False)
        calls = {"n": 0}

        async def _fake_chat_completion(_cfg, _system, user, response_format=None):
            del response_format
            calls["n"] += 1
            if calls["n"] == 1:
                return (
                    '{'
                    '"steps": ['
                    '{"id":"step1","title":"Сделать работу","instruction":"do","step_type":"task",'
                    '"depends_on":[],"parallel_group":null,"parallelizable":false,'
                    '"parallelizable_reason":null,"ask_question":null,"ask_options":null}'
                    ']'
                    '}'
                )
            assert "Обязательные входные данные:" in user
            assert "- Платформа" in user
            assert "- Ограничения" in user
            return """{
              "ask_question": "Какая платформа нужна в первой версии?",
              "ask_options": ["web", "mobile", "обе"]
            }"""

        monkeypatch.setattr(planner, "chat_completion", _fake_chat_completion)

        steps = await planner.plan_steps(
            cfg,
            "Сделай ТЗ",
            (
                "analyst_intent_flags:\n"
                '{"needs_clarification": true, "clarification_is_blocking": true, '
                '"clarification_topic": "Нужно уточнить платформу", '
                '"template_id": "change_spec", '
                '"required_inputs": ["Платформа", "Ограничения"]}'
            ),
        )
        assert calls["n"] == 2
        assert steps[0].ask_question == "Какая платформа нужна в первой версии?"
        assert steps[0].ask_options[:2] == ["web", "mobile"]

    asyncio.run(_run())


def test_planner_adds_external_reference_research_step_for_analyst_url(tmp_path, monkeypatch):
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

        monkeypatch.setattr(planner, "needs_clarification", lambda *_args, **_kwargs: False)

        async def _fake_chat_completion(_cfg, _system, _user, response_format=None):
            del response_format
            return (
                '{'
                '"steps": ['
                '{"id":"step1","title":"Собрать итоговый материал","instruction":"do","step_type":"task",'
                '"depends_on":[],"parallel_group":null,"parallelizable":false,'
                '"parallelizable_reason":null,"ask_question":null,"ask_options":null}'
                ']'
                '}'
            )

        monkeypatch.setattr(planner, "chat_completion", _fake_chat_completion)

        steps = await planner.plan_steps(
            cfg,
            "Подготовь ТЗ и используй внешний референс https://example.com/ref-spec",
            (
                "executor_profile=analyst\n"
                f"project_root={tmp_path}\n"
                f"workdir={tmp_path}\n"
                "analyst_intent_flags:\n"
                '{"document_kind": "spec", "requires_codebase_grounding": true, '
                '"requires_repo_audit": false, "requires_final_repo_review": false}'
            ),
        )

        assert [step.id for step in steps][:3] == [
            "use_cli_repo_grounding",
            "analyze_external_reference",
            "step1",
        ]
        external_step = next(step for step in steps if step.id == "analyze_external_reference")
        assert external_step.depends_on == ["use_cli_repo_grounding"]
        assert "https://example.com/ref-spec" in external_step.instruction
        assert "requires-validation" in external_step.instruction
        assert "local mapping" in external_step.instruction

    asyncio.run(_run())


def test_planner_does_not_add_external_reference_research_step_without_url(tmp_path, monkeypatch):
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

        monkeypatch.setattr(planner, "needs_clarification", lambda *_args, **_kwargs: False)

        async def _fake_chat_completion(_cfg, _system, _user, response_format=None):
            del response_format
            return (
                '{'
                '"steps": ['
                '{"id":"step1","title":"Собрать итоговый материал","instruction":"do","step_type":"task",'
                '"depends_on":[],"parallel_group":null,"parallelizable":false,'
                '"parallelizable_reason":null,"ask_question":null,"ask_options":null}'
                ']'
                '}'
            )

        monkeypatch.setattr(planner, "chat_completion", _fake_chat_completion)

        steps = await planner.plan_steps(
            cfg,
            "Подготовь ТЗ без внешнего референса",
            (
                "executor_profile=analyst\n"
                f"project_root={tmp_path}\n"
                f"workdir={tmp_path}\n"
                "analyst_intent_flags:\n"
                '{"document_kind": "spec", "requires_codebase_grounding": true, '
                '"requires_repo_audit": false, "requires_final_repo_review": false}'
            ),
        )

        assert "use_cli_repo_grounding" in [step.id for step in steps]
        assert "analyze_external_reference" not in [step.id for step in steps]

    asyncio.run(_run())
