import asyncio
import logging
import types

import pytest

from app.services.config_service import ConfigProvider, ConfigService
from app.services.mode_run_lifecycle_service import ModeRunLifecycleService
from app.services.run_artifact_store import RunArtifactStore
from app.services.session_service import SessionService
from app.services.task_service import TaskService
from config import AppConfig, DefaultsConfig, MCPConfig, MiniAppConfig, TelegramConfig, ToolConfig
from desktop.services.application_facade import ApplicationFacade
from session import SessionManager


class _InMemoryConfigProvider(ConfigProvider):
    def __init__(self, config: AppConfig):
        self.config = config

    async def load(self) -> AppConfig:
        return self.config

    async def get(self, key: str, default=None):
        current = self.config
        for part in str(key or "").split("."):
            token = part.strip()
            if not token:
                continue
            if isinstance(current, dict):
                if token not in current:
                    return default
                current = current[token]
                continue
            if not hasattr(current, token):
                return default
            current = getattr(current, token)
        return current


def _build_config(tmp_path) -> AppConfig:
    return AppConfig(
        telegram=TelegramConfig(token="t", whitelist_chat_ids=[1], admlist_chat_ids=[1]),
        tools={
            "dummy": ToolConfig(
                name="dummy",
                mode="headless",
                cmd=["bash", "-lc", "cat"],
            )
        },
        defaults=DefaultsConfig(
            workdir=str(tmp_path / "workdir"),
            state_path=str(tmp_path / "runtime" / "state.json"),
            toolhelp_path=str(tmp_path / "runtime" / "toolhelp.json"),
            log_path=str(tmp_path / "logs" / "bot.log"),
            run_artifacts_retention_days=21,
            skill_discovery_mode="suggest",
            skill_install_policy="manual",
            skill_registry_paths=[".cli-proxy/skills"],
            skill_allowlisted_sources=["local:global-registry", "registry:npx-skills"],
        ),
        mcp=MCPConfig(enabled=False),
        mcp_clients=[],
        presets=[],
        path=str(tmp_path / "config.yaml"),
        miniapp=MiniAppConfig(),
    )


def test_desktop_facade_list_runs_returns_recent_items_with_skill_log_and_limit(tmp_path) -> None:
    cfg = _build_config(tmp_path)
    (tmp_path / "workdir").mkdir(parents=True, exist_ok=True)
    (tmp_path / "runtime").mkdir(parents=True, exist_ok=True)
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)

    task_service = TaskService()
    session_manager = SessionManager(cfg)
    session_service = SessionService(session_manager, task_service)
    facade = ApplicationFacade(
        config_service=ConfigService(_InMemoryConfigProvider(cfg)),
        session_service=session_service,
        task_service=task_service,
    )
    facade.config = cfg
    session = session_service.create_session(1, "dummy", str(tmp_path / "workdir"))
    artifact_store = RunArtifactStore(cfg)

    first_run = artifact_store.start_run(
        session=session,
        mode_id="agent",
        run_id="run_20260312T120000Z_a1b2c3d4",
        phase="execute",
        source_prompt_hash="sha256:first-intent",
    )
    artifact_store.save_state(
        first_run,
        {
            "phase": "execute",
            "status": "running",
            "source_prompt_hash": "sha256:first-intent",
            "selected_skill_ids": ["playwright-cli"],
        },
    )
    artifact_store.append_event(
        first_run,
        {
            "event_type": "cli_skill_context_applied",
            "selected_skill_ids": ["playwright-cli"],
        },
    )

    second_run = artifact_store.start_run(
        session=session,
        mode_id="agent",
        run_id="run_20260312T120500Z_d4c3b2a1",
        phase="execute",
        source_prompt_hash="sha256:second-intent",
    )
    artifact_store.save_state(
        second_run,
        {
            "phase": "execute",
            "status": "running",
            "source_prompt_hash": "sha256:second-intent",
            "selected_skill_ids": ["playwright-cli", "xlsx"],
            "mode_context": {
                "cli_work_type": "implementation",
                "executor_profile": "default",
            },
        },
    )
    artifact_store.append_event(
        second_run,
        {
            "event_type": "cli_skill_context_applied",
            "selected_skill_ids": ["playwright-cli", "xlsx"],
        },
    )
    artifact_store.append_event(
        second_run,
        {
            "event_type": "skill_install",
            "skill_id": "xlsx",
            "status": "ok",
        },
    )

    third_run = artifact_store.start_run(
        session=session,
        mode_id="agent",
        run_id="run_20260312T120900Z_terminal01",
        phase="execute",
        source_prompt_hash="sha256:third-intent",
    )
    artifact_store.save_state(
        third_run,
        {
            "phase": "execute",
            "status": "terminated",
            "source_prompt_hash": "sha256:third-intent",
        },
    )

    listed_runs = facade.list_runs(session.conversation_scope.session_uid, limit=3)
    latest_only = facade.list_runs(session.conversation_scope.session_uid, limit=1)

    assert [item["run_id"] for item in listed_runs] == [third_run.run_id, second_run.run_id, first_run.run_id]
    assert listed_runs[0]["active"] is False
    assert listed_runs[0]["skill_log"] == []
    assert listed_runs[1]["skill_log"][:2] == [
        "Installed: xlsx",
        "Injected: playwright-cli, xlsx",
    ]
    assert listed_runs[1]["cli_work_type"] == "implementation"
    assert listed_runs[1]["executor_profile"] == "default"
    assert listed_runs[1]["project_local_skill_ids"] == []
    assert listed_runs[1]["active"] is True
    assert listed_runs[1]["run_operations_policy"]["doctor"]["allowed"] is True
    assert listed_runs[1]["run_operations_policy"]["recover"]["allowed"] is False
    assert listed_runs[1]["run_operations_policy"]["recover"]["reason"] == "admin_required"
    assert listed_runs[1]["run_operations_policy"]["resume"]["allowed"] is False
    assert listed_runs[1]["run_operations_policy"]["apply_recommendation"]["allowed"] is False
    assert listed_runs[1]["run_operations_policy"]["promote_skills"]["allowed"] is False
    assert listed_runs[2]["skill_log"] == ["Injected: playwright-cli"]
    assert listed_runs[2]["project_local_skill_ids"] == []
    assert latest_only == [listed_runs[0]]


def test_desktop_mode_dependencies_include_lifecycle_service_without_bot_app_expansion(tmp_path) -> None:
    cfg = _build_config(tmp_path)
    (tmp_path / "workdir").mkdir(parents=True, exist_ok=True)
    (tmp_path / "runtime").mkdir(parents=True, exist_ok=True)
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)

    task_service = TaskService()
    session_manager = SessionManager(cfg)
    session_service = SessionService(session_manager, task_service)
    facade = ApplicationFacade(
        config_service=ConfigService(_InMemoryConfigProvider(cfg)),
        session_service=session_service,
        task_service=task_service,
        mode_registry_service=types.SimpleNamespace(registry=object()),
    )
    facade.config = cfg

    deps = facade._desktop_mode_dependencies()

    assert isinstance(deps.mode_run_lifecycle, ModeRunLifecycleService)
    assert deps.mode_run_lifecycle.artifact_store is deps.run_artifacts.artifact_store
    assert deps.mode_run_lifecycle.observability is deps.run_observability
    assert deps.mode_run_lifecycle.boundary_validator is deps.run_boundary_validation
    assert not hasattr(facade._desktop_bot_app(), "mode_run_lifecycle")


def test_desktop_facade_list_runs_blocks_resume_and_recover_for_superseded_only(tmp_path) -> None:
    cfg = _build_config(tmp_path)
    (tmp_path / "workdir").mkdir(parents=True, exist_ok=True)
    (tmp_path / "runtime").mkdir(parents=True, exist_ok=True)
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)

    task_service = TaskService()
    session_manager = SessionManager(cfg)
    session_service = SessionService(session_manager, task_service)
    facade = ApplicationFacade(
        config_service=ConfigService(_InMemoryConfigProvider(cfg)),
        session_service=session_service,
        task_service=task_service,
    )
    facade.config = cfg
    session = session_service.create_session(1, "dummy", str(tmp_path / "workdir"))
    artifact_store = RunArtifactStore(cfg)

    superseded_run = artifact_store.start_run(
        session=session,
        mode_id="manager",
        run_id="run_20260312T121000Z_superseded",
        phase="complete",
        source_prompt_hash="sha256:superseded",
    )
    artifact_store.save_state(
        superseded_run,
        {
            "phase": "complete",
            "status": "superseded",
            "finished_at": 1.0,
        },
    )
    artifact_store.save_recovery(
        superseded_run,
        {
            "status": "needs_recovery",
            "recommended_action": "replay_finalize",
            "can_resume": True,
            "issues": [{"code": "boundary_contract_failed"}],
        },
    )

    failed_run = artifact_store.start_run(
        session=session,
        mode_id="manager",
        run_id="run_20260312T121100Z_failed",
        phase="complete",
        source_prompt_hash="sha256:failed",
    )
    artifact_store.save_state(
        failed_run,
        {
            "phase": "complete",
            "status": "failed",
            "finished_at": 2.0,
        },
    )
    artifact_store.save_recovery(
        failed_run,
        {
            "status": "needs_recovery",
            "recommended_action": "restart_from_phase",
            "can_resume": True,
            "issues": [{"code": "boundary_contract_failed"}],
        },
    )

    listed_runs = facade.list_runs(session.conversation_scope.session_uid, limit=2)
    runs_by_id = {item["run_id"]: item for item in listed_runs}

    assert runs_by_id[superseded_run.run_id]["terminal_status"] is True
    assert runs_by_id[superseded_run.run_id]["terminal_actions_blocked"] is True
    assert runs_by_id[superseded_run.run_id]["can_resume"] is False
    assert runs_by_id[superseded_run.run_id]["can_recover"] is False
    assert runs_by_id[failed_run.run_id]["terminal_status"] is True
    assert runs_by_id[failed_run.run_id]["terminal_actions_blocked"] is False
    assert runs_by_id[failed_run.run_id]["can_resume"] is True
    assert runs_by_id[failed_run.run_id]["can_recover"] is True


def test_desktop_facade_doctor_run_targets_specific_run_and_notifies(tmp_path) -> None:
    cfg = _build_config(tmp_path)
    (tmp_path / "workdir").mkdir(parents=True, exist_ok=True)
    (tmp_path / "runtime").mkdir(parents=True, exist_ok=True)
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)

    task_service = TaskService()
    session_manager = SessionManager(cfg)
    session_service = SessionService(session_manager, task_service)
    facade = ApplicationFacade(
        config_service=ConfigService(_InMemoryConfigProvider(cfg)),
        session_service=session_service,
        task_service=task_service,
    )
    facade.config = cfg
    session = session_service.create_session(1, "dummy", str(tmp_path / "workdir"))

    class _RunOperationsStub:
        def __init__(self):
            self.calls = []

        async def doctor_run(self, *, session, mode_id=None, run_id=None, context=None, dest=None):
            _ = context, dest
            self.calls.append((session, mode_id, run_id))
            return type(
                "_Result",
                (),
                {
                    "operation": "doctor",
                    "status": "ok",
                    "mode_id": str(mode_id or ""),
                    "phase": "execute",
                    "message": "Doctor готов.",
                    "run_id": str(run_id or ""),
                    "recommended_action": "resume_same_phase",
                    "blocked_by": (),
                    "report": {"status": "ok"},
                },
            )()

    stub = _RunOperationsStub()
    notifications = []
    facade.subscribe(lambda note: notifications.append(note))
    facade._desktop_run_operations_service = stub

    result = asyncio.run(
        facade.doctor_run(
            session.conversation_scope.session_uid,
            mode_id="agent",
            run_id="run_20260312T120500Z_d4c3b2a1",
        )
    )

    assert stub.calls == [(session, "agent", "run_20260312T120500Z_d4c3b2a1")]
    assert result["status"] == "ok"
    assert result["recommended_action"] == "resume_same_phase"
    assert any(
        note.event == "ui:runs_updated" and note.payload.get("run_id") == "run_20260312T120500Z_d4c3b2a1"
        for note in notifications
    )
    assert any(
        note.event == "ui:message" and note.payload.get("text") == "Doctor готов."
        for note in notifications
    )


def test_desktop_facade_apply_recommendation_targets_specific_run_and_notifies(tmp_path) -> None:
    cfg = _build_config(tmp_path)
    (tmp_path / "workdir").mkdir(parents=True, exist_ok=True)
    (tmp_path / "runtime").mkdir(parents=True, exist_ok=True)
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)

    task_service = TaskService()
    session_manager = SessionManager(cfg)
    session_service = SessionService(session_manager, task_service)
    facade = ApplicationFacade(
        config_service=ConfigService(_InMemoryConfigProvider(cfg)),
        session_service=session_service,
        task_service=task_service,
    )
    facade.config = cfg
    session = session_service.create_session(1, "dummy", str(tmp_path / "workdir"))

    class _RunOperationsStub:
        def __init__(self):
            self.calls = []

        async def apply_recommendation_run(self, *, session, mode_id=None, run_id=None, context=None, dest=None):
            _ = context, dest
            self.calls.append((session, mode_id, run_id))
            return type(
                "_Result",
                (),
                {
                    "operation": "apply_recommendation",
                    "status": "ok",
                    "mode_id": str(mode_id or ""),
                    "phase": "operation",
                    "message": "Validate operation executed.",
                    "run_id": str(run_id or ""),
                    "recommended_action": "run_validate",
                    "blocked_by": (),
                    "report": {"status": "needs_recovery"},
                },
            )()

    stub = _RunOperationsStub()
    notifications = []
    facade.subscribe(lambda note: notifications.append(note))
    facade._desktop_run_operations_service = stub
    facade.get_admin_status_payload = lambda _uid: {"active": True}  # type: ignore[method-assign]

    result = asyncio.run(
        facade.apply_recommendation_run(
            session.conversation_scope.session_uid,
            mode_id="codebase_mapper",
            run_id="run_20260312T120500Z_mapper",
        )
    )

    assert stub.calls == [(session, "codebase_mapper", "run_20260312T120500Z_mapper")]
    assert result["status"] == "ok"
    assert result["recommended_action"] == "run_validate"
    assert any(
        note.event == "ui:runs_updated" and note.payload.get("run_id") == "run_20260312T120500Z_mapper"
        for note in notifications
    )
    assert any(
        note.event == "ui:message" and note.payload.get("text") == "Validate operation executed."
        for note in notifications
    )


def test_desktop_facade_promote_run_skills_uses_shared_skill_runtime(tmp_path) -> None:
    cfg = _build_config(tmp_path)
    (tmp_path / "workdir").mkdir(parents=True, exist_ok=True)
    (tmp_path / "runtime").mkdir(parents=True, exist_ok=True)
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)

    task_service = TaskService()
    session_manager = SessionManager(cfg)
    session_service = SessionService(session_manager, task_service)
    facade = ApplicationFacade(
        config_service=ConfigService(_InMemoryConfigProvider(cfg)),
        session_service=session_service,
        task_service=task_service,
    )
    facade.config = cfg
    session = session_service.create_session(1, "dummy", str(tmp_path / "workdir"))

    class _SkillRuntimeStub:
        def __init__(self) -> None:
            self.calls = []

        def promote_run_skills(
            self,
            *,
            session,
            run_artifact_store,
            mode_id=None,
            run_id=None,
            is_admin=None,
            context=None,
            dest=None,
        ):
            self.calls.append((session, run_artifact_store, mode_id, run_id, is_admin, context, dest))
            return type(
                "_PromotionResult",
                (),
                {
                    "to_dict": lambda self: {
                        "status": "ok",
                        "message": "Skills promoted to global: playwright-cli",
                        "mode_id": str(mode_id or ""),
                        "run_id": str(run_id or ""),
                        "promoted_skill_ids": ["playwright-cli"],
                        "skipped_skill_ids": [],
                        "results": [],
                    }
                },
            )()

    notifications = []
    facade.subscribe(lambda note: notifications.append(note))
    facade.get_admin_status_payload = lambda _uid: {"active": True}  # type: ignore[method-assign]
    skill_runtime = _SkillRuntimeStub()
    facade._desktop_mode_dependencies_instance = type(
        "_Deps",
        (),
        {"skill_runtime": skill_runtime},
    )()
    facade._desktop_run_operations_service = type(
        "_RunOps",
        (),
        {"artifact_store": object()},
    )()

    result = asyncio.run(
        facade.promote_run_skills(
            session.conversation_scope.session_uid,
            mode_id="agent",
            run_id="run_20260313T150000Z_promote1",
        )
    )

    assert result["status"] == "ok"
    assert result["promoted_skill_ids"] == ["playwright-cli"]
    assert skill_runtime.calls[0][0] is session
    assert skill_runtime.calls[0][2:5] == ("agent", "run_20260313T150000Z_promote1", True)
    assert getattr(skill_runtime.calls[0][5], "transport", "") == "desktop"
    assert skill_runtime.calls[0][6]["kind"] == "desktop"
    assert any(
        note.event == "ui:runs_updated" and note.payload.get("operation") == "promote_run_skills"
        for note in notifications
    )
    assert any(
        note.event == "ui:message" and note.payload.get("text") == "Skills promoted to global: playwright-cli"
        for note in notifications
    )


@pytest.mark.parametrize(
    ("method_name", "operation"),
    [
        ("recover_run", "recover"),
        ("resume_run", "resume"),
        ("apply_recommendation_run", "apply_recommendation"),
        ("promote_run_skills", "promote_run_skills"),
    ],
)
def test_desktop_facade_denies_non_admin_write_run_operations_before_execution(
    tmp_path,
    method_name: str,
    operation: str,
) -> None:
    cfg = _build_config(tmp_path)
    (tmp_path / "workdir").mkdir(parents=True, exist_ok=True)
    (tmp_path / "runtime").mkdir(parents=True, exist_ok=True)
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)

    task_service = TaskService()
    session_manager = SessionManager(cfg)
    session_service = SessionService(session_manager, task_service)
    facade = ApplicationFacade(
        config_service=ConfigService(_InMemoryConfigProvider(cfg)),
        session_service=session_service,
        task_service=task_service,
    )
    facade.config = cfg
    session = session_service.create_session(1, "dummy", str(tmp_path / "workdir"))
    facade.get_admin_status_payload = lambda _uid: {"active": False}  # type: ignore[method-assign]

    class _RunOperationsStub:
        def __init__(self) -> None:
            self.calls = []
            self.artifact_store = object()

        async def recover_run(self, **kwargs):
            self.calls.append(("recover", kwargs))
            raise AssertionError("recover_run must be denied before service execution")

        async def resume_run(self, **kwargs):
            self.calls.append(("resume", kwargs))
            raise AssertionError("resume_run must be denied before service execution")

        async def apply_recommendation_run(self, **kwargs):
            self.calls.append(("apply_recommendation", kwargs))
            raise AssertionError("apply_recommendation_run must be denied before service execution")

    class _SkillRuntimeStub:
        def __init__(self) -> None:
            self.calls = []

        def promote_run_skills(self, **kwargs):
            self.calls.append(kwargs)
            raise AssertionError("promote_run_skills must be denied before runtime execution")

    run_ops = _RunOperationsStub()
    skill_runtime = _SkillRuntimeStub()
    facade._desktop_run_operations_service = run_ops
    facade._desktop_mode_dependencies_instance = type("_Deps", (), {"skill_runtime": skill_runtime})()
    notifications = []
    facade.subscribe(lambda note: notifications.append(note))

    method = getattr(facade, method_name)
    result = asyncio.run(
        method(
            session.conversation_scope.session_uid,
            mode_id="agent",
            run_id="run_20260313T150000Z_denied",
        )
    )

    assert result["status"] == "denied"
    assert result["policy"]["allowed"] is False
    assert result["policy"]["reason"] == "admin_required"
    assert result["policy"]["visibility"] == "hide"
    assert result["mode_id"] == "agent"
    assert result["run_id"] == "run_20260313T150000Z_denied"
    assert run_ops.calls == []
    assert skill_runtime.calls == []
    assert any(
        note.event == "ui:runs_updated"
        and note.payload.get("operation") == operation
        and note.payload.get("status") == "denied"
        for note in notifications
    )
    assert any(
        note.event == "ui:message"
        and "admin_required" in str(note.payload.get("text") or "")
        for note in notifications
    )
    assert result["policy"] == {
        "allowed": False,
        "reason": "admin_required",
        "visibility": "hide",
    }


def test_desktop_facade_run_policy_actor_fallback_logs_legacy_marker(tmp_path, caplog) -> None:
    cfg = _build_config(tmp_path)
    task_service = TaskService()
    session_manager = SessionManager(cfg)
    session_service = SessionService(session_manager, task_service)
    facade = ApplicationFacade(
        config_service=ConfigService(_InMemoryConfigProvider(cfg)),
        session_service=session_service,
        task_service=task_service,
    )
    facade._desktop_identity_provider_service = (  # type: ignore[method-assign]
        lambda: (_ for _ in ()).throw(RuntimeError("identity unavailable"))
    )
    session = types.SimpleNamespace(id="s1", chat_id=42)

    with caplog.at_level(logging.WARNING):
        user_id = facade._desktop_run_policy_user_id(session)

    assert user_id == 42
    assert "legacy fallback used: desktop run policy actor resolution failed" in caplog.text


def test_desktop_facade_skill_install_approval_actions_use_shared_runtime(tmp_path) -> None:
    cfg = _build_config(tmp_path)
    (tmp_path / "workdir").mkdir(parents=True, exist_ok=True)
    (tmp_path / "runtime").mkdir(parents=True, exist_ok=True)
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)

    task_service = TaskService()
    session_manager = SessionManager(cfg)
    session_service = SessionService(session_manager, task_service)
    facade = ApplicationFacade(
        config_service=ConfigService(_InMemoryConfigProvider(cfg)),
        session_service=session_service,
        task_service=task_service,
    )
    facade.config = cfg
    session = session_service.create_session(1, "dummy", str(tmp_path / "workdir"))

    class _SkillRuntimeStub:
        def __init__(self) -> None:
            self.list_calls = []
            self.approve_calls = []
            self.reject_calls = []

        def list_pending_installs(self, *, session):
            self.list_calls.append(session)
            return [
                type(
                    "_Pending",
                    (),
                    {
                        "approval_id": "approval-1",
                        "skill_id": "playwright-cli-local",
                        "mode_id": "agent",
                        "phase": "execute",
                        "source": "ref:owner-repo-skill",
                        "acquisition_source": "ref:owner-repo-skill",
                        "ref": "owner/repo@playwright-cli-local",
                        "created_at": 1_700_000_100.0,
                        "requester": {"actor_chat_id": "1"},
                    },
                )(),
            ]

        def approve_pending_install(self, *, session, approval_id, is_admin=None):
            self.approve_calls.append((session, approval_id, is_admin))
            return type(
                "_ApproveResult",
                (),
                {
                    "to_dict": lambda self: {
                        "status": "ok",
                        "approval_id": str(approval_id),
                        "skill_id": "playwright-cli-local",
                        "message": "Skill `playwright-cli-local` установлен локально после approve.",
                        "manifest_path": str(tmp_path / "workdir" / ".cli-proxy" / "skills" / "playwright-cli-local" / "SKILL.md"),
                    }
                },
            )()

        def reject_pending_install(self, *, session, approval_id, is_admin=None):
            self.reject_calls.append((session, approval_id, is_admin))
            return type(
                "_RejectResult",
                (),
                {
                    "to_dict": lambda self: {
                        "status": "ok",
                        "approval_id": str(approval_id),
                        "skill_id": "playwright-cli-local",
                        "message": "Pending установка skill `playwright-cli-local` отклонена.",
                        "manifest_path": None,
                    }
                },
            )()

    notifications = []
    facade.subscribe(lambda note: notifications.append(note))
    facade.get_admin_status_payload = lambda _uid: {"active": True}  # type: ignore[method-assign]
    skill_runtime = _SkillRuntimeStub()
    facade._desktop_mode_dependencies_instance = type("_Deps", (), {"skill_runtime": skill_runtime})()

    listed = facade.list_pending_skill_installs(session.conversation_scope.session_uid)
    approved = asyncio.run(
        facade.approve_pending_skill_install(
            session.conversation_scope.session_uid,
            approval_id="approval-1",
        )
    )
    rejected = asyncio.run(
        facade.reject_pending_skill_install(
            session.conversation_scope.session_uid,
            approval_id="approval-1",
        )
    )

    assert listed[0]["approval_id"] == "approval-1"
    assert listed[0]["skill_id"] == "playwright-cli-local"
    assert skill_runtime.list_calls == [session]

    assert approved["status"] == "ok"
    assert skill_runtime.approve_calls == [(session, "approval-1", True)]
    assert any(
        note.event == "ui:session_updated" and note.payload.get("operation") == "approve_pending_skill_install"
        for note in notifications
    )

    assert rejected["status"] == "ok"
    assert skill_runtime.reject_calls == [(session, "approval-1", True)]
    assert any(
        note.event == "ui:session_updated" and note.payload.get("operation") == "reject_pending_skill_install"
        for note in notifications
    )


def test_desktop_facade_skill_install_actions_require_active_admin_mode(tmp_path) -> None:
    cfg = _build_config(tmp_path)
    (tmp_path / "workdir").mkdir(parents=True, exist_ok=True)
    (tmp_path / "runtime").mkdir(parents=True, exist_ok=True)
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)

    task_service = TaskService()
    session_manager = SessionManager(cfg)
    session_service = SessionService(session_manager, task_service)
    facade = ApplicationFacade(
        config_service=ConfigService(_InMemoryConfigProvider(cfg)),
        session_service=session_service,
        task_service=task_service,
    )
    facade.config = cfg
    session = session_service.create_session(1, "dummy", str(tmp_path / "workdir"))
    facade.get_admin_status_payload = lambda _uid: {"active": False}  # type: ignore[method-assign]

    class _SkillRuntimeStub:
        def __init__(self) -> None:
            self.approve_calls = []

        def approve_pending_install(self, *, session, approval_id, is_admin=None):
            self.approve_calls.append((session, approval_id, is_admin))
            raise AssertionError("runtime should not be called without active admin mode")

    notifications = []
    facade.subscribe(lambda note: notifications.append(note))
    skill_runtime = _SkillRuntimeStub()
    facade._desktop_mode_dependencies_instance = type("_Deps", (), {"skill_runtime": skill_runtime})()

    listed = facade.list_pending_skill_installs(session.conversation_scope.session_uid)
    result = asyncio.run(
        facade.approve_pending_skill_install(
            session.conversation_scope.session_uid,
            approval_id="approval-1",
        )
    )

    assert listed == []
    assert result["status"] == "denied"
    assert skill_runtime.approve_calls == []
    assert any(
        note.event == "ui:session_updated" and note.payload.get("operation") == "approve_pending_skill_install"
        for note in notifications
    )
