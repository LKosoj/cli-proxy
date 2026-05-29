import asyncio
import time
import types

from modes.webmaster.mode import WebmasterMode
from modes.webmaster.models import FeedbackDecision, ValidationDecision
from modes.webmaster.state_store import WebmasterStateStore, build_user_key
from modes.sdk.json_store import write_json_locked


def _build_bot_app():
    return types.SimpleNamespace(
        config=types.SimpleNamespace(
            defaults=types.SimpleNamespace(
                webmaster_validation_max_fix_iterations=1,
            )
        )
    )


def _force_validation_pass(mode: WebmasterMode) -> None:
    def _parse(_self, _text):
        return ValidationDecision(
            status="PASS",
            summary="ok",
            blocking_issues=[],
            checklist_rows=[
                {
                    "item": "Семантический HTML",
                    "status": "PASS",
                    "evidence": "checked",
                    "fixed": "",
                    "why_not_done": "",
                }
            ],
            defects=[],
            raw={"status": "PASS"},
        )

    def _gate(_self, _decision, _developer_report):
        return True

    def _success(_self, developer_report):
        return str(developer_report or "")

    mode._parse_validation_report = types.MethodType(_parse, mode)
    mode._gate_passed = types.MethodType(_gate, mode)
    mode._build_success_message = types.MethodType(_success, mode)


def test_webmaster_classify_failure_sets_recovery_stage(tmp_path) -> None:
    async def _run() -> None:
        mode = WebmasterMode()
        store = WebmasterStateStore(str(tmp_path))

        def _fake_store(_self, _session=None):
            return store

        async def _fake_classify(_self, _bot_app, _user_text, _wm_ctx, *, session=None):
            raise RuntimeError("llm down")

        mode._store = types.MethodType(_fake_store, mode)
        mode._classify_feedback_llm = types.MethodType(_fake_classify, mode)

        session = types.SimpleNamespace(id="s1", workdir=str(tmp_path))
        bot_app = _build_bot_app()
        out = await mode.run_pipeline(
            session=session,
            user_text="Сделай правки на сайте",
            bot_app=bot_app,
            context=object(),
            dest={"kind": "telegram", "chat_id": 100, "user_id": 200},
        )

        assert "Не удалось обработать обратную связь" in out
        key = build_user_key(100, 200, "s1")
        wm_ctx = store.load(key)
        assert wm_ctx.stage == "await_intent_update"
        assert wm_ctx.last_user_text == "Сделай правки на сайте"

    asyncio.run(_run())


def test_webmaster_use_cli_failure_resets_running_stage(tmp_path) -> None:
    async def _run() -> None:
        mode = WebmasterMode()
        store = WebmasterStateStore(str(tmp_path))

        def _fake_store(_self, _session=None):
            return store

        async def _fake_classify(_self, _bot_app, _user_text, _wm_ctx, *, session=None):
            return FeedbackDecision(kind="continue_task", reason="ok")

        async def _fake_analyze_intent(_self, **_kwargs):
            return {
                "goal": "Обновить лендинг",
                "actions": ["Обновить hero"],
                "constraints": [],
                "acceptance_criteria": ["Текст обновлён"],
                "ambiguities": [],
                "assumptions": [],
            }

        async def _fake_confirm(_self, _bot_app, _session, _context, _dest, _wm_ctx):
            return "Подтвердить"

        async def _fake_use_cli(_self, _bot_app, _session, _context, _dest, _task_text, *, fresh_run):
            assert fresh_run is False
            raise RuntimeError("cli unavailable")

        def _fake_build_cli_task(_self, _wm_ctx, **_kwargs):
            return "task"

        mode._store = types.MethodType(_fake_store, mode)
        mode._classify_feedback_llm = types.MethodType(_fake_classify, mode)
        mode._analyze_intent = types.MethodType(_fake_analyze_intent, mode)
        mode._confirm_intent = types.MethodType(_fake_confirm, mode)
        mode._run_use_cli = types.MethodType(_fake_use_cli, mode)
        mode._build_cli_task = types.MethodType(_fake_build_cli_task, mode)
        _force_validation_pass(mode)
        _force_validation_pass(mode)

        session = types.SimpleNamespace(id="s2", workdir=str(tmp_path))
        bot_app = _build_bot_app()
        key = build_user_key(101, 201, "s2")
        wm_ctx = store.reset(key)
        wm_ctx.stage = "await_feedback"
        wm_ctx.last_cli_task = "previous_task"
        wm_ctx.last_cli_report = "previous_report"
        store.save(wm_ctx)
        out = await mode.run_pipeline(
            session=session,
            user_text="Обнови лендинг",
            bot_app=bot_app,
            context=object(),
            dest={"kind": "telegram", "chat_id": 101, "user_id": 201},
        )

        assert "Не удалось выполнить задачу в CLI" in out
        wm_ctx = store.load(key)
        assert wm_ctx.stage == "await_intent_update"
        assert wm_ctx.last_feedback_class == "continue_task"
        assert wm_ctx.last_cli_task == "task"
        assert wm_ctx.last_cli_report == "previous_report"

    asyncio.run(_run())


def test_webmaster_first_message_continue_task_forced_to_fresh_run(tmp_path) -> None:
    async def _run() -> None:
        mode = WebmasterMode()
        store = WebmasterStateStore(str(tmp_path))

        def _fake_store(_self, _session=None):
            return store

        async def _fake_classify(_self, _bot_app, _user_text, _wm_ctx, *, session=None):
            return FeedbackDecision(kind="continue_task", reason="classifier drift")

        async def _fake_analyze_intent(_self, **_kwargs):
            return {
                "goal": "Сделать правки",
                "actions": ["Обновить секцию"],
                "constraints": [],
                "acceptance_criteria": ["Секция обновлена"],
                "ambiguities": [],
                "assumptions": [],
            }

        async def _fake_confirm(_self, _bot_app, _session, _context, _dest, _wm_ctx):
            return "Подтвердить"

        async def _fake_use_cli(_self, _bot_app, _session, _context, _dest, _task_text, *, fresh_run):
            assert fresh_run is True
            return "ok"

        def _fake_build_cli_task(_self, _wm_ctx, **_kwargs):
            return "task"

        mode._store = types.MethodType(_fake_store, mode)
        mode._classify_feedback_llm = types.MethodType(_fake_classify, mode)
        mode._analyze_intent = types.MethodType(_fake_analyze_intent, mode)
        mode._confirm_intent = types.MethodType(_fake_confirm, mode)
        mode._run_use_cli = types.MethodType(_fake_use_cli, mode)
        mode._build_cli_task = types.MethodType(_fake_build_cli_task, mode)
        _force_validation_pass(mode)

        session = types.SimpleNamespace(id="s5", workdir=str(tmp_path))
        bot_app = _build_bot_app()
        out = await mode.run_pipeline(
            session=session,
            user_text="Сделай изменения",
            bot_app=bot_app,
            context=object(),
            dest={"kind": "telegram", "chat_id": 104, "user_id": 204},
        )

        assert out == "ok"
        key = build_user_key(104, 204, "s5")
        wm_ctx = store.load(key)
        assert wm_ctx.task_kind == "new_task"
        assert wm_ctx.last_feedback_class == "continue_task"

    asyncio.run(_run())


def test_webmaster_requirement_change_uses_fresh_run(tmp_path) -> None:
    async def _run() -> None:
        mode = WebmasterMode()
        store = WebmasterStateStore(str(tmp_path))

        def _fake_store(_self, _session=None):
            return store

        async def _fake_classify(_self, _bot_app, _user_text, _wm_ctx, *, session=None):
            return FeedbackDecision(kind="requirement_change", reason="changed scope")

        async def _fake_analyze_intent(_self, **_kwargs):
            return {
                "goal": "Сделать новый лендинг",
                "actions": ["Создать новый hero"],
                "constraints": [],
                "acceptance_criteria": ["Новый hero опубликован"],
                "ambiguities": [],
                "assumptions": [],
            }

        async def _fake_confirm(_self, _bot_app, _session, _context, _dest, _wm_ctx):
            return "Подтвердить"

        async def _fake_use_cli(_self, _bot_app, _session, _context, _dest, _task_text, *, fresh_run):
            assert fresh_run is True
            return "ok"

        def _fake_build_cli_task(_self, _wm_ctx, **_kwargs):
            return "task"

        mode._store = types.MethodType(_fake_store, mode)
        mode._classify_feedback_llm = types.MethodType(_fake_classify, mode)
        mode._analyze_intent = types.MethodType(_fake_analyze_intent, mode)
        mode._confirm_intent = types.MethodType(_fake_confirm, mode)
        mode._run_use_cli = types.MethodType(_fake_use_cli, mode)
        mode._build_cli_task = types.MethodType(_fake_build_cli_task, mode)
        _force_validation_pass(mode)

        session = types.SimpleNamespace(id="s3", workdir=str(tmp_path))
        bot_app = _build_bot_app()
        out = await mode.run_pipeline(
            session=session,
            user_text="Переделай всё на новую структуру",
            bot_app=bot_app,
            context=object(),
            dest={"kind": "telegram", "chat_id": 102, "user_id": 202},
        )

        assert out == "ok"
        key = build_user_key(102, 202, "s3")
        wm_ctx = store.load(key)
        assert wm_ctx.task_kind == "new_task"
        assert wm_ctx.last_feedback_class == "requirement_change"

    asyncio.run(_run())


def test_webmaster_unclear_feedback_returns_clarification_without_cli(tmp_path) -> None:
    async def _run() -> None:
        mode = WebmasterMode()
        store = WebmasterStateStore(str(tmp_path))

        def _fake_store(_self, _session=None):
            return store

        async def _fake_classify(_self, _bot_app, _user_text, _wm_ctx, *, session=None):
            return FeedbackDecision(kind="unclear", reason="ambiguous request")

        async def _should_not_analyze(_self, **_kwargs):
            raise AssertionError("analyze_intent must not be called for unclear feedback")

        async def _should_not_use_cli(_self, *_args, **_kwargs):
            raise AssertionError("use_cli must not be called for unclear feedback")

        mode._store = types.MethodType(_fake_store, mode)
        mode._classify_feedback_llm = types.MethodType(_fake_classify, mode)
        mode._analyze_intent = types.MethodType(_should_not_analyze, mode)
        mode._run_use_cli = types.MethodType(_should_not_use_cli, mode)

        session = types.SimpleNamespace(id="s4", workdir=str(tmp_path))
        bot_app = _build_bot_app()
        out = await mode.run_pipeline(
            session=session,
            user_text="ну сделай как надо",
            bot_app=bot_app,
            context=object(),
            dest={"kind": "telegram", "chat_id": 103, "user_id": 203},
        )

        assert "Запрос пока неясен" in out
        key = build_user_key(103, 203, "s4")
        wm_ctx = store.load(key)
        assert wm_ctx.stage == "await_intent_update"
        assert wm_ctx.last_feedback_class == "unclear"

    asyncio.run(_run())


def test_webmaster_wrong_execution_patch_build_failure_is_non_fatal(tmp_path) -> None:
    async def _run() -> None:
        mode = WebmasterMode()
        store = WebmasterStateStore(str(tmp_path))

        def _fake_store(_self, _session=None):
            return store

        async def _fake_classify(_self, _bot_app, _user_text, _wm_ctx, *, session=None):
            return FeedbackDecision(kind="wrong_execution", reason="needs patch")

        async def _fake_analyze_intent(_self, **_kwargs):
            return {
                "goal": "Обновить сайт",
                "actions": ["Обновить hero"],
                "constraints": [],
                "acceptance_criteria": ["Hero обновлен"],
                "ambiguities": [],
                "assumptions": [],
            }

        async def _fake_confirm(_self, _bot_app, _session, _context, _dest, _wm_ctx):
            return "Подтвердить"

        async def _fake_use_cli(_self, _bot_app, _session, _context, _dest, _task_text, *, fresh_run):
            return "ok"

        async def _fail_patch(_self, _bot_app, _user_feedback, _cli_report, *, session=None):
            raise RuntimeError("llm down")

        def _fake_build_cli_task(_self, _wm_ctx, **_kwargs):
            return "task"

        mode._store = types.MethodType(_fake_store, mode)
        mode._classify_feedback_llm = types.MethodType(_fake_classify, mode)
        mode._analyze_intent = types.MethodType(_fake_analyze_intent, mode)
        mode._confirm_intent = types.MethodType(_fake_confirm, mode)
        mode._run_use_cli = types.MethodType(_fake_use_cli, mode)
        mode._build_prompt_patch_llm = types.MethodType(_fail_patch, mode)
        mode._build_cli_task = types.MethodType(_fake_build_cli_task, mode)
        _force_validation_pass(mode)

        session = types.SimpleNamespace(id="s6", workdir=str(tmp_path))
        bot_app = _build_bot_app()
        key = build_user_key(105, 205, "s6")
        wm_ctx = store.reset(key)
        wm_ctx.stage = "await_feedback"
        wm_ctx.last_cli_report = "previous report"
        store.save(wm_ctx)

        out = await mode.run_pipeline(
            session=session,
            user_text="Сделал не то, исправь",
            bot_app=bot_app,
            context=object(),
            dest={"kind": "telegram", "chat_id": 105, "user_id": 205},
        )

        assert out == "ok"
        wm_ctx = store.load(key)
        assert wm_ctx.stage == "await_feedback"
        assert wm_ctx.last_feedback_class == "wrong_execution"

    asyncio.run(_run())


def test_webmaster_wrong_execution_invalid_json_patch_is_non_fatal(tmp_path) -> None:
    async def _run() -> None:
        mode = WebmasterMode()
        store = WebmasterStateStore(str(tmp_path))

        def _fake_store(_self, _session=None):
            return store

        async def _fake_classify(_self, _bot_app, _user_text, _wm_ctx, *, session=None):
            return FeedbackDecision(kind="wrong_execution", reason="invalid json")

        async def _fake_analyze_intent(_self, **_kwargs):
            return {
                "goal": "Обновить сайт",
                "actions": ["Обновить hero"],
                "constraints": [],
                "acceptance_criteria": ["Hero обновлен"],
                "ambiguities": [],
                "assumptions": [],
            }

        async def _fake_confirm(_self, _bot_app, _session, _context, _dest, _wm_ctx):
            return "Подтвердить"

        async def _fake_use_cli(_self, _bot_app, _session, _context, _dest, _task_text, *, fresh_run):
            return "ok"

        async def _fail_patch_json(_self, _bot_app, _user_feedback, _cli_report, *, session=None):
            raise RuntimeError("LLM returned invalid JSON")

        def _fake_build_cli_task(_self, _wm_ctx, **_kwargs):
            return "task"

        mode._store = types.MethodType(_fake_store, mode)
        mode._classify_feedback_llm = types.MethodType(_fake_classify, mode)
        mode._analyze_intent = types.MethodType(_fake_analyze_intent, mode)
        mode._confirm_intent = types.MethodType(_fake_confirm, mode)
        mode._run_use_cli = types.MethodType(_fake_use_cli, mode)
        mode._build_prompt_patch_llm = types.MethodType(_fail_patch_json, mode)
        mode._build_cli_task = types.MethodType(_fake_build_cli_task, mode)
        _force_validation_pass(mode)

        session = types.SimpleNamespace(id="s7", workdir=str(tmp_path))
        bot_app = _build_bot_app()
        key = build_user_key(106, 206, "s7")
        wm_ctx = store.reset(key)
        wm_ctx.stage = "await_feedback"
        wm_ctx.last_cli_report = "previous report"
        store.save(wm_ctx)

        out = await mode.run_pipeline(
            session=session,
            user_text="Исправь, разбор был плохой",
            bot_app=bot_app,
            context=object(),
            dest={"kind": "telegram", "chat_id": 106, "user_id": 206},
        )

        assert out == "ok"
        wm_ctx = store.load(key)
        assert wm_ctx.stage == "await_feedback"
        assert wm_ctx.last_feedback_class == "wrong_execution"

    asyncio.run(_run())


def test_webmaster_wrong_execution_prompt_learning_save_failure_is_non_fatal(tmp_path) -> None:
    async def _run() -> None:
        mode = WebmasterMode()
        store = WebmasterStateStore(str(tmp_path))

        def _fake_store(_self, _session=None):
            return store

        async def _fake_classify(_self, _bot_app, _user_text, _wm_ctx, *, session=None):
            return FeedbackDecision(kind="wrong_execution", reason="save failed")

        async def _fake_analyze_intent(_self, **_kwargs):
            return {
                "goal": "Обновить сайт",
                "actions": ["Обновить hero"],
                "constraints": [],
                "acceptance_criteria": ["Hero обновлен"],
                "ambiguities": [],
                "assumptions": [],
            }

        async def _fake_confirm(_self, _bot_app, _session, _context, _dest, _wm_ctx):
            return "Подтвердить"

        async def _fake_use_cli(_self, _bot_app, _session, _context, _dest, _task_text, *, fresh_run):
            return "ok"

        async def _fake_patch(_self, _bot_app, _user_feedback, _cli_report, *, session=None):
            return {
                "added_rules": "- rule",
                "changed_rules": "",
                "removed_rules": "",
                "reason": "r",
                "expected_effect": "e",
            }

        def _fake_build_cli_task(_self, _wm_ctx, **_kwargs):
            return "task"

        original_save = store.save_prompt_learning

        def _fail_save(_data):
            raise RuntimeError("disk error")

        mode._store = types.MethodType(_fake_store, mode)
        mode._classify_feedback_llm = types.MethodType(_fake_classify, mode)
        mode._analyze_intent = types.MethodType(_fake_analyze_intent, mode)
        mode._confirm_intent = types.MethodType(_fake_confirm, mode)
        mode._run_use_cli = types.MethodType(_fake_use_cli, mode)
        mode._build_prompt_patch_llm = types.MethodType(_fake_patch, mode)
        mode._build_cli_task = types.MethodType(_fake_build_cli_task, mode)
        _force_validation_pass(mode)

        session = types.SimpleNamespace(id="s8", workdir=str(tmp_path))
        bot_app = _build_bot_app()
        key = build_user_key(107, 207, "s8")
        wm_ctx = store.reset(key)
        wm_ctx.stage = "await_feedback"
        wm_ctx.last_cli_report = "previous report"
        store.save(wm_ctx)

        store.save_prompt_learning = _fail_save  # type: ignore[assignment]
        try:
            out = await mode.run_pipeline(
                session=session,
                user_text="Сохрани обучение",
                bot_app=bot_app,
                context=object(),
                dest={"kind": "telegram", "chat_id": 107, "user_id": 207},
            )
        finally:
            store.save_prompt_learning = original_save  # type: ignore[assignment]

        assert out == "ok"
        wm_ctx = store.load(key)
        assert wm_ctx.stage == "await_feedback"
        assert wm_ctx.last_feedback_class == "wrong_execution"

    asyncio.run(_run())


def test_webmaster_resume_running_dev_cli_uses_saved_task_and_iteration(tmp_path) -> None:
    async def _run() -> None:
        mode = WebmasterMode()
        store = WebmasterStateStore(str(tmp_path / ".cli-proxy" / ".webmaster_data"))

        def _fake_store(_self, _session=None):
            return store

        async def _should_not_classify(_self, _bot_app, _user_text, _wm_ctx, *, session=None):
            raise AssertionError("classify must not be called in resume path")

        calls: list[dict[str, object]] = []

        async def _fake_use_cli(_self, _bot_app, _session, _context, _dest, task_text, *, fresh_run):
            calls.append({"task_text": str(task_text), "fresh_run": bool(fresh_run)})
            if len(calls) == 1:
                assert fresh_run is False
                assert "resume-dev-task" in str(task_text)
                return (
                    "Отчет\n\n"
                    "| Пункт | Статус (PASS|PARTIAL|FAIL) | Как проверено / доказательство | Что исправлено | Почему не выполнено |\n"
                    "| --- | --- | --- | --- | --- |\n"
                    "| Семантический HTML | PASS | checked | updated | |\n"
                )
            assert fresh_run is True
            return (
                '{"status":"PASS","summary":"ok","blocking_issues":[],'
                '"checklist_results":[{"item":"Семантический HTML","status":"PASS","evidence":"checked",'
                '"fixed":"updated","why_not_done":""}],"defects":[]}'
            )

        mode._store = types.MethodType(_fake_store, mode)
        mode._classify_feedback_llm = types.MethodType(_should_not_classify, mode)
        mode._run_use_cli = types.MethodType(_fake_use_cli, mode)

        session = types.SimpleNamespace(id="s-resume-dev", workdir=str(tmp_path))
        key = build_user_key(201, 301, "s-resume-dev")
        wm_ctx = store.reset(key)
        wm_ctx.stage = "running_dev_cli"
        wm_ctx.task_kind = "continue_task"
        wm_ctx.last_feedback_class = "continue_task"
        wm_ctx.last_cli_task = "resume-dev-task"
        wm_ctx.fix_iteration_count = 1
        wm_ctx.updated_at = time.time()
        store.save(wm_ctx)

        out = await mode.run_pipeline(
            session=session,
            user_text="продолжай после перезапуска",
            bot_app=_build_bot_app(),
            context=object(),
            dest={"kind": "telegram", "chat_id": 201, "user_id": 301},
        )

        assert out.startswith("✅ Задача выполнена и валидация пройдена.")
        saved = store.load(key)
        assert saved.stage == "await_feedback"
        assert saved.fix_iteration_count == 1
        assert len(calls) == 2
        assert calls[0]["task_text"] == "resume-dev-task"
        assert calls[0]["fresh_run"] is False
        assert calls[1]["fresh_run"] is True

    asyncio.run(_run())


def test_webmaster_resume_running_validation_cli_reuses_saved_report(tmp_path) -> None:
    async def _run() -> None:
        mode = WebmasterMode()
        store = WebmasterStateStore(str(tmp_path / ".cli-proxy" / ".webmaster_data"))

        def _fake_store(_self, _session=None):
            return store

        async def _should_not_classify(_self, _bot_app, _user_text, _wm_ctx, *, session=None):
            raise AssertionError("classify must not be called in resume path")

        calls: list[dict[str, object]] = []

        async def _fake_use_cli(_self, _bot_app, _session, _context, _dest, task_text, *, fresh_run):
            calls.append({"task_text": str(task_text), "fresh_run": bool(fresh_run)})
            assert fresh_run is True
            return (
                '{"status":"PASS","summary":"ok","blocking_issues":[],'
                '"checklist_results":[{"item":"Семантический HTML","status":"PASS","evidence":"checked",'
                '"fixed":"updated","why_not_done":""}],"defects":[]}'
            )

        mode._store = types.MethodType(_fake_store, mode)
        mode._classify_feedback_llm = types.MethodType(_should_not_classify, mode)
        mode._run_use_cli = types.MethodType(_fake_use_cli, mode)

        session = types.SimpleNamespace(id="s-resume-validation", workdir=str(tmp_path))
        key = build_user_key(202, 302, "s-resume-validation")
        wm_ctx = store.reset(key)
        wm_ctx.stage = "running_validation_cli"
        wm_ctx.task_kind = "continue_task"
        wm_ctx.last_feedback_class = "continue_task"
        wm_ctx.last_cli_task = "resume-dev-task"
        wm_ctx.last_cli_report = (
            "Отчет\n\n"
            "| Пункт | Статус (PASS|PARTIAL|FAIL) | Как проверено / доказательство | Что исправлено | Почему не выполнено |\n"
            "| --- | --- | --- | --- | --- |\n"
            "| Семантический HTML | PASS | checked | updated | |\n"
        )
        wm_ctx.fix_iteration_count = 2
        wm_ctx.updated_at = time.time()
        store.save(wm_ctx)

        out = await mode.run_pipeline(
            session=session,
            user_text="продолжай после перезапуска",
            bot_app=_build_bot_app(),
            context=object(),
            dest={"kind": "telegram", "chat_id": 202, "user_id": 302},
        )

        assert out.startswith("✅ Задача выполнена и валидация пройдена.")
        saved = store.load(key)
        assert saved.stage == "await_feedback"
        assert saved.fix_iteration_count == 2
        assert len(calls) == 1
        assert calls[0]["fresh_run"] is True

    asyncio.run(_run())


def test_webmaster_resume_expired_ttl_resets_state_to_idle(tmp_path) -> None:
    async def _run() -> None:
        mode = WebmasterMode()
        store = WebmasterStateStore(str(tmp_path / ".cli-proxy" / ".webmaster_data"))

        def _fake_store(_self, _session=None):
            return store

        async def _should_not_classify(_self, _bot_app, _user_text, _wm_ctx, *, session=None):
            raise AssertionError("classify must not be called in expired resume path")

        async def _should_not_use_cli(_self, *_args, **_kwargs):
            raise AssertionError("use_cli must not be called when resume ttl expired")

        mode._store = types.MethodType(_fake_store, mode)
        mode._classify_feedback_llm = types.MethodType(_should_not_classify, mode)
        mode._run_use_cli = types.MethodType(_should_not_use_cli, mode)

        session = types.SimpleNamespace(id="s-resume-expired", workdir=str(tmp_path))
        key = build_user_key(203, 303, "s-resume-expired")
        wm_ctx = store.reset(key)
        wm_ctx.stage = "running_dev_cli"
        wm_ctx.last_cli_task = "resume-dev-task"
        wm_ctx.fix_iteration_count = 3
        store.save(wm_ctx)
        wm_ctx = store.load(key)
        wm_ctx.updated_at = time.time() - 8_000
        write_json_locked(store.path_for(key), wm_ctx.to_dict())

        out = await mode.run_pipeline(
            session=session,
            user_text="продолжай после перезапуска",
            bot_app=_build_bot_app(),
            context=object(),
            dest={"kind": "telegram", "chat_id": 203, "user_id": 303},
        )

        assert "контекст устарел по TTL" in out
        saved = store.load(key)
        assert saved.stage == "idle"
        assert saved.fix_iteration_count == 0

    asyncio.run(_run())


def test_webmaster_parallel_sessions_isolated_by_workdir_and_checkpoint_policy(tmp_path) -> None:
    async def _run() -> None:
        mode = WebmasterMode()
        _force_validation_pass(mode)

        async def _checkpoint(_self, _session, _label: str):
            return True

        async def _fake_classify(_self, _bot_app, _user_text, _wm_ctx, *, session=None):
            await asyncio.sleep(0.01)
            return FeedbackDecision(kind="continue_task", reason="parallel-run")

        async def _fake_analyze_intent(_self, **kwargs):
            user_text = str(kwargs.get("user_text") or "").strip()
            return {
                "goal": f"goal::{user_text}",
                "actions": [f"action::{user_text}"],
                "constraints": [],
                "acceptance_criteria": [f"accept::{user_text}"],
                "ambiguities": [],
                "assumptions": [],
            }

        async def _fake_confirm(_self, _bot_app, _session, _context, _dest, _wm_ctx):
            return "Подтвердить"

        def _fake_build_cli_task(_self, _wm_ctx, **_kwargs):
            return str(_wm_ctx.goal or "")

        def _fake_build_validation_task(_self, _wm_ctx, _developer_report, *, session=None):
            return "validate"

        checkpoint_calls: list[tuple[str, str, str]] = []
        use_cli_calls: list[tuple[str, str, bool, str]] = []

        async def _fake_run_use_cli(
            _self,
            _bot_app,
            _session,
            _context,
            _dest,
            task_text,
            *,
            fresh_run,
        ):
            sid = str(getattr(_session, "id", ""))
            wd = str(getattr(_session, "workdir", ""))
            task = str(task_text or "")
            use_cli_calls.append((sid, wd, bool(fresh_run), task))
            await asyncio.sleep(0.01)
            if task.startswith("goal::"):
                return f"developer-report::{task}"
            return "validation-ok"

        async def _checkpoint_with_log(_self, _session, label: str):
            checkpoint_calls.append(
                (
                    str(getattr(_session, "id", "")),
                    str(getattr(_session, "workdir", "")),
                    str(label),
                )
            )
            return await _checkpoint(_self, _session, label)

        mode._classify_feedback_llm = types.MethodType(_fake_classify, mode)
        mode._analyze_intent = types.MethodType(_fake_analyze_intent, mode)
        mode._confirm_intent = types.MethodType(_fake_confirm, mode)
        mode._build_cli_task = types.MethodType(_fake_build_cli_task, mode)
        mode._build_validation_task = types.MethodType(_fake_build_validation_task, mode)
        mode._run_use_cli = types.MethodType(_fake_run_use_cli, mode)
        mode._silent_git_checkpoint = types.MethodType(_checkpoint_with_log, mode)

        workdir_a = tmp_path / "wd_a"
        workdir_b = tmp_path / "wd_b"
        workdir_a.mkdir(parents=True, exist_ok=True)
        workdir_b.mkdir(parents=True, exist_ok=True)

        session_a = types.SimpleNamespace(id="shared-session", workdir=str(workdir_a))
        session_b = types.SimpleNamespace(id="shared-session", workdir=str(workdir_b))
        bot_app = _build_bot_app()
        dest = {"kind": "telegram", "chat_id": 901, "user_id": 902}
        key = build_user_key(901, 902, "shared-session")

        out_a, out_b = await asyncio.gather(
            mode.run_pipeline(
                session=session_a,
                user_text="task-a",
                bot_app=bot_app,
                context=object(),
                dest=dict(dest),
            ),
            mode.run_pipeline(
                session=session_b,
                user_text="task-b",
                bot_app=bot_app,
                context=object(),
                dest=dict(dest),
            ),
        )

        assert "developer-report::goal::task-a" in out_a
        assert "developer-report::goal::task-b" in out_b

        store_a = mode._store(session_a)
        store_b = mode._store(session_b)
        saved_a = store_a.load(key)
        saved_b = store_b.load(key)
        assert saved_a.stage == "await_feedback"
        assert saved_b.stage == "await_feedback"
        assert saved_a.goal == "goal::task-a"
        assert saved_b.goal == "goal::task-b"
        assert saved_a.last_cli_task == "goal::task-a"
        assert saved_b.last_cli_task == "goal::task-b"
        assert saved_a.last_cli_task != saved_b.last_cli_task

        path_a = store_a.path_for(key)
        path_b = store_b.path_for(key)
        assert path_a != path_b

        reloaded = WebmasterMode()
        loaded_a = reloaded._store(session_a).load(key)
        loaded_b = reloaded._store(session_b).load(key)
        assert loaded_a.last_cli_task == "goal::task-a"
        assert loaded_b.last_cli_task == "goal::task-b"

        labels_a = [label for _sid, wd, label in checkpoint_calls if wd == str(workdir_a)]
        labels_b = [label for _sid, wd, label in checkpoint_calls if wd == str(workdir_b)]
        assert labels_a == ["before_start", "before_response_success"]
        assert labels_b == ["before_start", "before_response_success"]

        dev_runs = [
            (sid, wd)
            for sid, wd, _is_fresh, task in use_cli_calls
            if task.startswith("goal::")
        ]
        validation_runs = [
            (sid, wd)
            for sid, wd, is_fresh, task in use_cli_calls
            if bool(is_fresh) and "validate" in task
        ]
        assert dev_runs.count(("shared-session", str(workdir_a))) == 1
        assert dev_runs.count(("shared-session", str(workdir_b))) == 1
        assert validation_runs.count(("shared-session", str(workdir_a))) == 1
        assert validation_runs.count(("shared-session", str(workdir_b))) == 1

    asyncio.run(_run())


def test_webmaster_parallel_resume_ttl_isolated_between_workdirs(tmp_path) -> None:
    async def _run() -> None:
        mode = WebmasterMode()
        _force_validation_pass(mode)

        async def _checkpoint(_self, _session, _label: str):
            return True

        async def _should_not_classify(_self, _bot_app, _user_text, _wm_ctx, *, session=None):
            raise AssertionError("classify must not be called in resume path")

        def _fake_build_validation_task(_self, _wm_ctx, _developer_report, *, session=None):
            return "validate"

        use_cli_calls: list[tuple[str, str, bool, str]] = []

        async def _fake_run_use_cli(
            _self,
            _bot_app,
            _session,
            _context,
            _dest,
            task_text,
            *,
            fresh_run,
        ):
            sid = str(getattr(_session, "id", ""))
            wd = str(getattr(_session, "workdir", ""))
            task = str(task_text or "")
            use_cli_calls.append((sid, wd, bool(fresh_run), task))
            if not bool(fresh_run):
                assert task == "resume-task-a"
                return "developer-report::resume-task-a"
            return "validation-ok"

        mode._classify_feedback_llm = types.MethodType(_should_not_classify, mode)
        mode._build_validation_task = types.MethodType(_fake_build_validation_task, mode)
        mode._run_use_cli = types.MethodType(_fake_run_use_cli, mode)
        mode._silent_git_checkpoint = types.MethodType(_checkpoint, mode)

        workdir_a = tmp_path / "ttl_a"
        workdir_b = tmp_path / "ttl_b"
        workdir_a.mkdir(parents=True, exist_ok=True)
        workdir_b.mkdir(parents=True, exist_ok=True)

        session_a = types.SimpleNamespace(id="shared-session", workdir=str(workdir_a))
        session_b = types.SimpleNamespace(id="shared-session", workdir=str(workdir_b))
        key = build_user_key(777, 888, "shared-session")

        store_a = mode._store(session_a)
        store_b = mode._store(session_b)
        wm_ctx_a = store_a.reset(key)
        wm_ctx_a.stage = "running_dev_cli"
        wm_ctx_a.last_cli_task = "resume-task-a"
        wm_ctx_a.fix_iteration_count = 1
        store_a.save(wm_ctx_a)
        wm_ctx_a = store_a.load(key)
        wm_ctx_a.updated_at = time.time() - 30
        write_json_locked(store_a.path_for(key), wm_ctx_a.to_dict())

        wm_ctx_b = store_b.reset(key)
        wm_ctx_b.stage = "running_dev_cli"
        wm_ctx_b.last_cli_task = "resume-task-b"
        wm_ctx_b.fix_iteration_count = 5
        store_b.save(wm_ctx_b)
        wm_ctx_b = store_b.load(key)
        wm_ctx_b.updated_at = time.time() - 10_000
        write_json_locked(store_b.path_for(key), wm_ctx_b.to_dict())

        bot_app = types.SimpleNamespace(
            config=types.SimpleNamespace(
                defaults=types.SimpleNamespace(
                    webmaster_validation_max_fix_iterations=1,
                    webmaster_resume_ttl_sec=120,
                )
            )
        )
        dest = {"kind": "telegram", "chat_id": 777, "user_id": 888}
        out_a, out_b = await asyncio.gather(
            mode.run_pipeline(
                session=session_a,
                user_text="resume-a",
                bot_app=bot_app,
                context=object(),
                dest=dict(dest),
            ),
            mode.run_pipeline(
                session=session_b,
                user_text="resume-b",
                bot_app=bot_app,
                context=object(),
                dest=dict(dest),
            ),
        )

        assert "developer-report::resume-task-a" in out_a
        assert "контекст устарел по TTL" in out_b

        saved_a = store_a.load(key)
        saved_b = store_b.load(key)
        assert saved_a.stage == "await_feedback"
        assert saved_a.fix_iteration_count == 1
        assert saved_b.stage == "idle"
        assert saved_b.fix_iteration_count == 0

        non_fresh_calls = [
            (sid, wd, task)
            for sid, wd, is_fresh, task in use_cli_calls
            if not is_fresh
        ]
        assert non_fresh_calls == [("shared-session", str(workdir_a), "resume-task-a")]
        assert all(str(workdir_b) != wd for _sid, wd, _task in non_fresh_calls)

    asyncio.run(_run())
