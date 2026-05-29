from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from app.services.skill_registry_service import SkillRegistryService
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
        conversation_scope=SimpleNamespace(session_uid=f"thread:-100:{case_id}"),
    )


def _write_skill(root: Path, skill_id: str, *, title: str, description: str) -> Path:
    path = root / skill_id / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "---",
                f"name: {title}",
                f"description: {description}",
                "---",
                "",
                f"# {title}",
                "",
                description,
                "",
            ]
        ),
        encoding="utf-8",
    )
    return path


def test_skill_registry_project_definitions_shadow_global_collisions(tmp_path) -> None:
    cfg = _build_config(tmp_path, intent="shadow")
    session = _session(tmp_path, case_id="shadow")
    global_root = Path(cfg.defaults.workdir) / ".cli-proxy" / "skills"
    project_root = Path(session.workdir) / ".cli-proxy" / "skills"

    global_shared = _write_skill(
        global_root,
        "playwright-cli",
        title="Global Playwright",
        description="global definition",
    )
    _write_skill(global_root, "xlsx", title="Global XLSX", description="global spreadsheet helper")
    project_shared = _write_skill(
        project_root,
        "playwright-cli",
        title="Project Playwright",
        description="project override definition",
    )
    _write_skill(project_root, "billing-domain", title="Billing Domain", description="project domain helper")

    service = SkillRegistryService(cfg)
    snapshot = service.load_registry(session=session)

    assert service.scan_global_registry()["playwright-cli"].manifest_path == str(global_shared)
    assert service.scan_project_registry(session)["playwright-cli"].manifest_path == str(project_shared)
    assert snapshot.effective_manifests["playwright-cli"].manifest_path == str(project_shared)
    assert snapshot.effective_manifests["playwright-cli"].source == "local:project-registry"
    assert snapshot.effective_manifests["playwright-cli"].description == "project override definition"
    assert "xlsx" in snapshot.effective_manifests
    assert "billing-domain" in snapshot.effective_manifests
    assert snapshot.collisions["playwright-cli"] == [str(global_shared), str(project_shared)]


def test_skill_registry_contract_isolates_effective_sets_between_sessions(tmp_path) -> None:
    cfg = _build_config(tmp_path, intent="isolation")
    session_a = _session(tmp_path, case_id="intent-a")
    session_b = _session(tmp_path, case_id="intent-b")
    global_root = Path(cfg.defaults.workdir) / ".cli-proxy" / "skills"
    _write_skill(global_root, "shared", title="Shared", description="shared helper")
    _write_skill(Path(session_a.workdir) / ".cli-proxy" / "skills", "alpha", title="Alpha", description="alpha helper")
    _write_skill(Path(session_b.workdir) / ".cli-proxy" / "skills", "beta", title="Beta", description="beta helper")

    service = SkillRegistryService(cfg)
    snapshot_a = service.load_registry(session=session_a)
    snapshot_b = service.load_registry(session=session_b)

    assert "alpha" in snapshot_a.effective_manifests
    assert "beta" not in snapshot_a.effective_manifests
    assert "beta" in snapshot_b.effective_manifests
    assert "alpha" not in snapshot_b.effective_manifests
    assert snapshot_a.available_skill_set_hash() != snapshot_b.available_skill_set_hash()
