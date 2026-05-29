from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import yaml

from app.services.skill_policy_service import SkillPolicyService
from app.services.skill_registry_service import SkillManifest
from config import AppConfig, DefaultsConfig, MCPConfig, MiniAppConfig, TelegramConfig, ToolConfig


def _build_config(tmp_path: Path, *, intent: str) -> AppConfig:
    workdir = tmp_path / f"global_{intent}"
    runtime = tmp_path / f"runtime_{intent}"
    logs = tmp_path / f"logs_{intent}"
    workdir.mkdir(parents=True, exist_ok=True)
    runtime.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    return AppConfig(
        telegram=TelegramConfig(token="token", whitelist_chat_ids=[1], admlist_chat_ids=[1]),
        tools={"dummy": ToolConfig(name="dummy", mode="headless", cmd=["bash", "-lc", "cat"])},
        defaults=DefaultsConfig(
            workdir=str(workdir),
            state_path=str(runtime / "state.json"),
            toolhelp_path=str(runtime / "toolhelp.json"),
            log_path=str(logs / "bot.log"),
            skill_registry_paths=[".cli-proxy/skills"],
            skill_allowlisted_sources=["local:global-registry", "local:project-registry"],
        ),
        mcp=MCPConfig(enabled=False),
        mcp_clients=[],
        presets=[],
        path=str(tmp_path / f"config_{intent}.yaml"),
        miniapp=MiniAppConfig(),
    )


def _session(tmp_path: Path, *, case_id: str) -> SimpleNamespace:
    workdir = tmp_path / f"project_{case_id}"
    workdir.mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(
        id=f"s-{case_id}",
        workdir=str(workdir),
        project_root=str(workdir),
        conversation_scope=SimpleNamespace(session_uid=f"thread:-100:{case_id}"),
    )


def test_skill_policy_merges_preferences_global_then_project_override(tmp_path) -> None:
    cfg = _build_config(tmp_path, intent="prefs")
    session = _session(tmp_path, case_id="prefs")
    global_prefs = Path(cfg.defaults.workdir) / ".cli-proxy" / "skills" / "preferences.yaml"
    project_prefs = Path(session.workdir) / ".cli-proxy" / "skills" / "preferences.yaml"
    global_prefs.parent.mkdir(parents=True, exist_ok=True)
    project_prefs.parent.mkdir(parents=True, exist_ok=True)
    global_prefs.write_text(
        yaml.safe_dump(
            {
                "always_use_skills": ["global-skill"],
                "prefer_skills": ["playwright-cli"],
                "skill_discovery_mode": "suggest",
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    project_prefs.write_text(
        yaml.safe_dump(
            {
                "always_use_skills": ["local-skill"],
                "avoid_skills": ["legacy-skill"],
                "skill_install_policy": "admin_approve",
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    service = SkillPolicyService(cfg)
    preferences = service.load_preferences(session=session)

    assert preferences.always_use_skills == ("local-skill",)
    assert preferences.prefer_skills == ("playwright-cli",)
    assert preferences.avoid_skills == ("legacy-skill",)
    assert preferences.skill_discovery_mode == "suggest"
    assert preferences.skill_install_policy == "admin_approve"


def test_skill_policy_rejects_non_allowlisted_origins(tmp_path) -> None:
    cfg = _build_config(tmp_path, intent="allowlist")
    session = _session(tmp_path, case_id="allowlist")
    service = SkillPolicyService(cfg)
    local_manifest = SkillManifest(
        skill_id="local-skill",
        title="Local Skill",
        description="allowed local skill",
        source="local:project-registry",
        scope="project",
        root_path=str(Path(session.workdir) / ".cli-proxy" / "skills"),
        skill_path=str(Path(session.workdir) / ".cli-proxy" / "skills" / "local-skill"),
        manifest_path=str(Path(session.workdir) / ".cli-proxy" / "skills" / "local-skill" / "SKILL.md"),
    )
    remote_manifest = SkillManifest(
        skill_id="remote-skill",
        title="Remote Skill",
        description="remote skill",
        source="registry:npx-skills",
        scope="global",
        root_path="/tmp/registry",
        skill_path="/tmp/registry/remote-skill",
        manifest_path="/tmp/registry/remote-skill/SKILL.md",
    )

    evaluation = service.evaluate_manifests(
        {
            local_manifest.skill_id: local_manifest,
            remote_manifest.skill_id: remote_manifest,
        },
        session=session,
    )

    assert service.validate_manifest(local_manifest).allowed is True
    assert service.validate_manifest(remote_manifest).allowed is False
    assert "local-skill" in evaluation.allowed_manifests
    assert "remote-skill" not in evaluation.allowed_manifests
    assert evaluation.rejected == [
        service.validate_manifest(remote_manifest),
    ]
    assert evaluation.rejected[0].reason == "source_not_allowlisted:registry:npx-skills"


def test_skill_policy_resolves_project_local_install_target_by_default(tmp_path) -> None:
    cfg = _build_config(tmp_path, intent="install_target")
    session = _session(tmp_path, case_id="install_target")
    service = SkillPolicyService(cfg)

    target = service.resolve_install_target(session=session)

    assert target == str(Path(session.workdir) / ".cli-proxy" / "skills")


def test_skill_policy_admin_approve_registers_pending_and_reject_lockout_by_task_iteration(tmp_path) -> None:
    cfg = _build_config(tmp_path, intent="approval_pending")
    session = _session(tmp_path, case_id="approval_pending")
    service = SkillPolicyService(cfg)

    initial = service.evaluate_install_request(
        session=session,
        mode_id="agent",
        phase="execute",
        task_hash="sha256:task-a",
        skill_id="github-ui-skill",
    )
    pending = service.register_pending_install(
        session=session,
        mode_id="agent",
        phase="execute",
        task_hash="sha256:task-a",
        skill_id="github-ui-skill",
        source="registry:npx-skills",
        acquisition_source="ref:owner-repo-skill",
        ref="acme/skills@github-ui-skill",
        install_target=service.resolve_install_target(session=session),
        requester={"session_uid": session.conversation_scope.session_uid},
        origin_payload={"candidate": {"skill_id": "github-ui-skill"}},
    )
    existing = service.evaluate_install_request(
        session=session,
        mode_id="agent",
        phase="execute",
        task_hash="sha256:task-a",
        skill_id="github-ui-skill",
    )
    rejected = service.reject_pending_install(
        session=session,
        approval_id=pending.approval_id if pending is not None else "",
        resolved_by={"actor_chat_id": "1"},
    )
    lockout = service.evaluate_install_request(
        session=session,
        mode_id="agent",
        phase="execute",
        task_hash="sha256:task-a",
        skill_id="github-ui-skill",
    )
    next_task = service.evaluate_install_request(
        session=session,
        mode_id="agent",
        phase="execute",
        task_hash="sha256:task-b",
        skill_id="github-ui-skill",
    )

    assert initial.status == "approval_required"
    assert pending is not None
    assert pending.status == "pending"
    assert pending.requester["session_uid"] == session.conversation_scope.session_uid
    assert pending.origin_payload["candidate"]["skill_id"] == "github-ui-skill"
    assert existing.status == "pending_existing"
    assert existing.record is not None
    assert existing.record.approval_id == pending.approval_id
    assert rejected is not None
    assert rejected.status == "rejected"
    assert service.get_pending_install(session=session, approval_id=pending.approval_id) is None
    assert lockout.status == "rejected_lockout"
    assert lockout.record is not None
    assert lockout.record.skill_id == "github-ui-skill"
    assert next_task.status == "approval_required"


def test_skill_policy_accept_pending_install_clears_pending_ledger(tmp_path) -> None:
    cfg = _build_config(tmp_path, intent="approval_accept")
    session = _session(tmp_path, case_id="approval_accept")
    service = SkillPolicyService(cfg)
    pending = service.register_pending_install(
        session=session,
        mode_id="agent",
        phase="execute",
        task_hash="sha256:task-accept",
        skill_id="playwright-cli",
        source="registry:npx-skills",
        acquisition_source="ref:owner-repo-skill",
        ref="acme/skills@playwright-cli",
        install_target=service.resolve_install_target(session=session),
        requester={"session_uid": session.conversation_scope.session_uid},
        origin_payload={"candidate": {"skill_id": "playwright-cli"}},
    )

    accepted = service.accept_pending_install(
        session=session,
        approval_id=pending.approval_id if pending is not None else "",
        resolved_by={"actor_chat_id": "1"},
    )

    assert pending is not None
    assert accepted is not None
    assert accepted.status == "approved"
    assert accepted.resolution_reason == "approved_by_admin"
    assert service.get_pending_install(session=session, approval_id=pending.approval_id) is None
    assert service.list_pending_installs(session=session) == []
