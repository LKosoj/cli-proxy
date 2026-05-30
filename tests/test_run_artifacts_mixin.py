"""
Unit-тесты для modes/sdk/run_artifacts_mixin.py.

Запуск (изолированно, без реального BotApp):
    .venv/bin/python -m pytest -q tests/test_run_artifacts_mixin.py
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
import pytest

from modes.sdk.run_artifacts_mixin import RunArtifactsMixin


# ---------------------------------------------------------------------------
# Фиктивный хост-класс
# ---------------------------------------------------------------------------

class _FakeRunArtifactsService:
    def __init__(self, enabled: bool = True):
        self._enabled = enabled

    def is_enabled(self) -> bool:
        return self._enabled


class _FakeArtifactStore:
    """Минимальный stub RunArtifactStore для тестов."""

    def __init__(self):
        self._state: Dict[str, Any] = {}
        self._plan: Dict[str, Any] = {}
        self._checkpoints: List[Dict[str, Any]] = []
        self._events: List[Dict[str, Any]] = []
        self._finished: Optional[Dict[str, Any]] = None

    def load_state(self, run: Any) -> Dict[str, Any]:
        return dict(self._state)

    def save_state(self, run: Any, state: Dict[str, Any]) -> None:
        self._state = dict(state)

    def save_plan(self, run: Any, plan: Dict[str, Any]) -> None:
        self._plan = dict(plan)

    def load_plan(self, run: Any) -> Dict[str, Any]:
        return dict(self._plan)

    def append_checkpoint(self, run: Any, checkpoint: Dict[str, Any]) -> None:
        self._checkpoints.append(dict(checkpoint))

    def append_event(self, run: Any, event: Dict[str, Any]) -> None:
        self._events.append(dict(event))

    def mark_finished(self, run: Any, *, status: str, phase: str) -> None:
        self._finished = {"status": status, "phase": phase}

    def latest_run(self, *, session: Any, mode_id: str) -> Any:
        return None


class _FakeRun:
    """Stub RunArtifactHandle."""
    run_id = "test-run-001"
    checkpoints_path = ""
    metrics_path = ""


class _FakeBoundaryValidator:
    def __init__(self, enabled: bool = True, status: str = "ok"):
        self._enabled = enabled
        self._status = status
        self.issues: List[Any] = []

    def is_enabled(self) -> bool:
        return self._enabled

    def validate(self, run: Any, *, mode_id: str, phase: str) -> Any:
        report = SimpleNamespace(status=self._status, issues=self.issues)
        return report


class _HostMode(RunArtifactsMixin):
    """
    Минимальный хост-класс, реализующий duck-typing-интерфейс для RunArtifactsMixin.
    """
    mode_id = "test_mode"

    def __init__(self):
        import logging
        self._log = logging.getLogger(__name__)
        self.config = None  # по умолчанию _artifact_store() вернёт None
        self._run_artifacts_svc: Optional[_FakeRunArtifactsService] = None
        self._boundary_validator: Optional[_FakeBoundaryValidator] = None
        self._fake_store: Optional[_FakeArtifactStore] = None
        self._run_doctor_svc: Optional[Any] = None

    def _optional_run_artifacts(self) -> Optional[_FakeRunArtifactsService]:
        return self._run_artifacts_svc

    def _optional_run_doctor(self) -> Optional[Any]:
        return self._run_doctor_svc

    def _optional_run_boundary_validation(self) -> Optional[_FakeBoundaryValidator]:
        return self._boundary_validator

    def _artifact_store(self) -> Optional[_FakeArtifactStore]:  # type: ignore[override]
        """Переопределён для возврата fake store из теста."""
        return self._fake_store


# ---------------------------------------------------------------------------
# Тесты _prompt_hash
# ---------------------------------------------------------------------------

class TestPromptHash:
    def test_deterministic(self):
        h1 = RunArtifactsMixin._prompt_hash("hello world")
        h2 = RunArtifactsMixin._prompt_hash("hello world")
        assert h1 == h2

    def test_starts_with_prefix(self):
        h = RunArtifactsMixin._prompt_hash("test")
        assert h.startswith("sha256:")

    def test_different_inputs_differ(self):
        h1 = RunArtifactsMixin._prompt_hash("a")
        h2 = RunArtifactsMixin._prompt_hash("b")
        assert h1 != h2

    def test_empty_string(self):
        h = RunArtifactsMixin._prompt_hash("")
        assert h.startswith("sha256:")
        assert len(h) == len("sha256:") + 64

    def test_none_coercion(self):
        # Метод принимает str, но кодовая база передаёт "" или str(x)
        h = RunArtifactsMixin._prompt_hash("")
        assert isinstance(h, str)


# ---------------------------------------------------------------------------
# Тесты _is_terminal_run_status
# ---------------------------------------------------------------------------

class TestIsTerminalRunStatus:
    @pytest.mark.parametrize("status", [
        "completed", "failed", "aborted", "canceled", "cancelled",
        "superseded", "terminated",
    ])
    def test_terminal_statuses(self, status):
        assert RunArtifactsMixin._is_terminal_run_status(status) is True

    @pytest.mark.parametrize("status", [
        "running", "idle", "initializing", "pending", "", None,
    ])
    def test_non_terminal_statuses(self, status):
        assert RunArtifactsMixin._is_terminal_run_status(status) is False

    def test_case_insensitive(self):
        assert RunArtifactsMixin._is_terminal_run_status("COMPLETED") is True
        assert RunArtifactsMixin._is_terminal_run_status("Failed") is True


# ---------------------------------------------------------------------------
# Тесты _save_run_state — shallow vs deep merge
# ---------------------------------------------------------------------------

class TestSaveRunState:
    def _make_host_with_store(self) -> tuple[_HostMode, _FakeArtifactStore]:
        host = _HostMode()
        store = _FakeArtifactStore()
        store._state = {
            "phase": "intent",
            "status": "running",
            "mode_context": {
                "execution_context": {
                    "chat_id": 100,
                    "nested": {"key_a": "old_a", "key_b": "old_b"},
                },
                "top_level_key": "original",
            },
        }
        host._fake_store = store
        return host, store

    def test_shallow_merge_overwrites_nested(self):
        host, store = self._make_host_with_store()
        run = _FakeRun()
        host._save_run_state(
            run,
            phase="intent",
            status="running",
            mode_context={
                "execution_context": {
                    "chat_id": 200,
                    "nested": {"key_a": "new_a"},
                }
            },
            merge_execution_context="shallow",
        )
        saved = store._state
        ec = saved["mode_context"]["execution_context"]
        # shallow: nested полностью заменяется входящим
        assert ec["chat_id"] == 200
        assert ec["nested"] == {"key_a": "new_a"}  # key_b потерялся — это shallow

    def test_deep_merge_preserves_nested_keys(self):
        host, store = self._make_host_with_store()
        run = _FakeRun()
        host._save_run_state(
            run,
            phase="intent",
            status="running",
            mode_context={
                "execution_context": {
                    "chat_id": 200,
                    "nested": {"key_a": "new_a"},
                }
            },
            merge_execution_context="deep",
        )
        saved = store._state
        ec = saved["mode_context"]["execution_context"]
        # deep: key_b из существующего должен сохраниться
        assert ec["chat_id"] == 200
        assert ec["nested"]["key_a"] == "new_a"
        assert ec["nested"]["key_b"] == "old_b"

    def test_top_level_mode_context_merged(self):
        host, store = self._make_host_with_store()
        run = _FakeRun()
        host._save_run_state(
            run,
            phase="intent",
            status="running",
            mode_context={"new_key": "new_value"},
        )
        saved = store._state
        # Исходный top_level_key должен сохраниться
        assert saved["mode_context"]["top_level_key"] == "original"
        assert saved["mode_context"]["new_key"] == "new_value"

    def test_none_run_is_noop(self):
        host = _HostMode()
        store = _FakeArtifactStore()
        host._fake_store = store
        host._save_run_state(None, phase="intent", status="running")
        assert store._state == {}  # не изменилось

    def test_no_artifact_store_is_noop(self):
        host = _HostMode()
        host._fake_store = None
        run = _FakeRun()
        # Не должно бросать исключение
        host._save_run_state(run, phase="intent", status="running")

    def test_phase_and_status_saved(self):
        host, store = self._make_host_with_store()
        run = _FakeRun()
        host._save_run_state(run, phase="complete", status="completed")
        assert store._state["phase"] == "complete"
        assert store._state["status"] == "completed"

    def test_default_merge_strategy_is_shallow(self):
        """Дефолт должен быть shallow — ключ key_b теряется."""
        host, store = self._make_host_with_store()
        run = _FakeRun()
        host._save_run_state(
            run,
            phase="intent",
            status="running",
            mode_context={"execution_context": {"nested": {"key_a": "x"}}},
        )
        ec = store._state["mode_context"]["execution_context"]
        assert "key_b" not in ec["nested"]


# ---------------------------------------------------------------------------
# Тесты _deep_merge_execution_context (статический метод)
# ---------------------------------------------------------------------------

class TestDeepMerge:
    def test_simple_overwrite(self):
        result = RunArtifactsMixin._deep_merge_execution_context(
            {"a": 1, "b": 2},
            {"b": 99, "c": 3},
        )
        assert result == {"a": 1, "b": 99, "c": 3}

    def test_nested_dict_merged(self):
        result = RunArtifactsMixin._deep_merge_execution_context(
            {"a": {"x": 1, "y": 2}},
            {"a": {"x": 10, "z": 3}},
        )
        assert result["a"] == {"x": 10, "y": 2, "z": 3}

    def test_nested_non_dict_overwrites(self):
        result = RunArtifactsMixin._deep_merge_execution_context(
            {"a": {"x": 1}},
            {"a": "string"},
        )
        assert result["a"] == "string"

    def test_empty_incoming(self):
        existing = {"a": 1, "b": {"c": 2}}
        result = RunArtifactsMixin._deep_merge_execution_context(existing, {})
        assert result == existing

    def test_empty_existing(self):
        incoming = {"a": 1}
        result = RunArtifactsMixin._deep_merge_execution_context({}, incoming)
        assert result == incoming

    def test_does_not_mutate_existing(self):
        existing = {"a": {"x": 1}}
        incoming = {"a": {"y": 2}}
        RunArtifactsMixin._deep_merge_execution_context(existing, incoming)
        assert existing == {"a": {"x": 1}}  # не мутировано


# ---------------------------------------------------------------------------
# Тесты _set_active_run_handle / _active_run_handle / _clear_active_run_handle
# ---------------------------------------------------------------------------

def _make_run_handle(run_id: str = "test-run") -> Any:
    """Конструирует RunArtifactHandle с полным набором полей."""
    from app.services.run_artifact_store import RunArtifactHandle
    return RunArtifactHandle(
        root_dir="",
        session_uid="",
        mode_id="test_mode",
        run_id=run_id,
        run_dir="",
        state_path="",
        plan_path="",
        checkpoints_path="",
        recovery_path="",
        metrics_path="",
        events_path="",
        artifacts_dir="",
        scratch_dir="",
    )


class TestRunHandleSessionAttr:
    def _make_session(self) -> SimpleNamespace:
        return SimpleNamespace()

    def test_default_attr_set_and_get(self):
        host = _HostMode()
        session = self._make_session()
        run = _make_run_handle("r1")
        host._set_active_run_handle(session, run)
        assert host._active_run_handle(session) is run

    def test_default_attr_clear(self):
        host = _HostMode()
        session = self._make_session()
        run = _make_run_handle("r2")
        host._set_active_run_handle(session, run)
        host._clear_active_run_handle(session)
        assert host._active_run_handle(session) is None

    def test_custom_attr_name_used(self):
        """_RUN_HANDLE_SESSION_ATTR переопределён — должен использоваться кастомный атрибут."""
        class _CustomHost(_HostMode):
            _RUN_HANDLE_SESSION_ATTR = "_my_custom_run_handle"

        host = _CustomHost()
        session = self._make_session()
        run = _make_run_handle("r3")
        host._set_active_run_handle(session, run)
        # Кастомный атрибут должен быть установлен
        assert getattr(session, "_my_custom_run_handle") is run
        # Стандартный атрибут НЕ должен быть установлен
        assert not hasattr(session, "_mode_active_run_handle")

    def test_active_run_handle_returns_none_if_wrong_type(self):
        host = _HostMode()
        session = self._make_session()
        setattr(session, host._RUN_HANDLE_SESSION_ATTR, "not_a_handle")  # type: ignore[arg-type]
        assert host._active_run_handle(session) is None

    def test_active_run_handle_returns_none_if_no_attr(self):
        host = _HostMode()
        session = self._make_session()
        assert host._active_run_handle(session) is None

    def test_clear_noop_if_attr_not_set(self):
        host = _HostMode()
        session = self._make_session()
        # Должно пройти без ошибок
        host._clear_active_run_handle(session)


# ---------------------------------------------------------------------------
# Тесты _is_run_artifacts_enabled
# ---------------------------------------------------------------------------

class TestIsRunArtifactsEnabled:
    def test_returns_true_when_enabled(self):
        host = _HostMode()
        host._run_artifacts_svc = _FakeRunArtifactsService(enabled=True)
        assert host._is_run_artifacts_enabled() is True

    def test_returns_false_when_disabled(self):
        host = _HostMode()
        host._run_artifacts_svc = _FakeRunArtifactsService(enabled=False)
        assert host._is_run_artifacts_enabled() is False

    def test_returns_false_when_service_none(self):
        host = _HostMode()
        host._run_artifacts_svc = None
        assert host._is_run_artifacts_enabled() is False

    def test_returns_false_when_service_raises(self):
        class _BrokenSvc:
            def is_enabled(self):
                raise RuntimeError("broken")

        host = _HostMode()
        host._run_artifacts_svc = _BrokenSvc()
        assert host._is_run_artifacts_enabled() is False


# ---------------------------------------------------------------------------
# Тесты _artifact_store
# ---------------------------------------------------------------------------

class TestArtifactStore:
    def test_returns_none_when_no_config(self):
        host = _HostMode()
        host.config = None
        # Переопределённый метод в _HostMode возвращает _fake_store,
        # но базовая реализация вернула бы None. Тестируем оригинал явно.
        result = RunArtifactsMixin._artifact_store(host)
        assert result is None

    def test_returns_store_when_config_present(self, tmp_path: Path):
        from config import AppConfig, DefaultsConfig, MCPConfig, MiniAppConfig, TelegramConfig
        workdir = tmp_path / "workdir"
        workdir.mkdir()
        config = AppConfig(
            telegram=TelegramConfig(token="t", whitelist_chat_ids=[1], admlist_chat_ids=[1]),
            tools={},
            defaults=DefaultsConfig(
                workdir=str(workdir),
                state_path=str(tmp_path / "state.json"),
                toolhelp_path=str(tmp_path / "help.json"),
                log_path=str(tmp_path / "bot.log"),
            ),
            mcp=MCPConfig(enabled=False),
            mcp_clients=[],
            presets=[],
            path=str(tmp_path / "config.yaml"),
            miniapp=MiniAppConfig(),
        )
        host = _HostMode()
        host.config = config
        from app.services.run_artifact_store import RunArtifactStore
        result = RunArtifactsMixin._artifact_store(host)
        assert isinstance(result, RunArtifactStore)


# ---------------------------------------------------------------------------
# Тесты _save_run_plan / _append_checkpoint / _append_run_event
# ---------------------------------------------------------------------------

class TestStoreOperations:
    def _host_with_store(self) -> tuple[_HostMode, _FakeArtifactStore]:
        host = _HostMode()
        store = _FakeArtifactStore()
        store._state = {"phase": "intent", "status": "running", "mode_context": {}}
        host._fake_store = store
        return host, store

    def test_save_run_plan(self):
        host, store = self._host_with_store()
        run = _FakeRun()
        host._save_run_plan(run, {"kind": "test_plan", "units": []})
        assert store._plan == {"kind": "test_plan", "units": []}

    def test_save_run_plan_none_run_noop(self):
        host, store = self._host_with_store()
        host._save_run_plan(None, {"kind": "noop"})
        assert store._plan == {}

    def test_append_checkpoint(self):
        host, store = self._host_with_store()
        run = _FakeRun()
        host._append_checkpoint(run, {"phase": "intent", "status": "ok"})
        assert len(store._checkpoints) == 1
        assert store._checkpoints[0]["phase"] == "intent"

    def test_append_run_event(self):
        host, store = self._host_with_store()
        run = _FakeRun()
        host._append_run_event(run, {"event_type": "start", "ts": 123})
        assert len(store._events) == 1
        assert store._events[0]["event_type"] == "start"

    def test_mark_run_finished(self):
        host, store = self._host_with_store()
        run = _FakeRun()
        host._mark_run_finished(run, status="completed", phase="complete")
        assert store._finished == {"status": "completed", "phase": "complete"}

    def test_mark_run_finished_none_run_noop(self):
        host, store = self._host_with_store()
        host._mark_run_finished(None, status="completed", phase="complete")
        assert store._finished is None


# ---------------------------------------------------------------------------
# Тесты _validate_run_boundary
# ---------------------------------------------------------------------------

class TestValidateRunBoundary:
    def test_ok_status_no_raise(self):
        host = _HostMode()
        store = _FakeArtifactStore()
        store._state = {"phase": "intent", "status": "running", "mode_context": {}}
        host._fake_store = store
        host._boundary_validator = _FakeBoundaryValidator(enabled=True, status="ok")
        run = _FakeRun()
        # Не должно бросать исключение
        host._validate_run_boundary(run, phase="intent")

    def test_non_ok_status_raises(self):
        host = _HostMode()
        store = _FakeArtifactStore()
        store._state = {"phase": "intent", "status": "running", "mode_context": {}}
        host._fake_store = store
        validator = _FakeBoundaryValidator(enabled=True, status="failed")
        issue = SimpleNamespace(code="missing_evidence")
        validator.issues = [issue]
        host._boundary_validator = validator
        run = _FakeRun()
        with pytest.raises(RuntimeError, match="Run boundary validation failed"):
            host._validate_run_boundary(run, phase="intent")

    def test_none_run_noop(self):
        host = _HostMode()
        host._boundary_validator = _FakeBoundaryValidator(enabled=True, status="failed")
        host._validate_run_boundary(None, phase="intent")

    def test_validator_disabled_noop(self):
        host = _HostMode()
        validator = _FakeBoundaryValidator(enabled=False, status="failed")
        validator.issues = [SimpleNamespace(code="x")]
        host._boundary_validator = validator
        run = _FakeRun()
        # Не должно бросать исключение
        host._validate_run_boundary(run, phase="intent")


# ---------------------------------------------------------------------------
# Тесты _latest_mode_run (простая версия)
# ---------------------------------------------------------------------------

class TestLatestModeRun:
    def test_returns_none_when_no_store(self):
        host = _HostMode()
        host._fake_store = None
        session = SimpleNamespace()
        assert host._latest_mode_run(session) is None

    def test_delegates_to_store(self):
        host = _HostMode()
        store = _FakeArtifactStore()
        expected = _make_run_handle("latest")

        def _latest_run(*, session: Any, mode_id: str) -> Any:
            return expected

        store.latest_run = _latest_run
        host._fake_store = store
        session = SimpleNamespace()
        result = host._latest_mode_run(session)
        assert result is expected
