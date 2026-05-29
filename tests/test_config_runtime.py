from __future__ import annotations

import asyncio
import copy
import hashlib
from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace

import yaml

from app.services import config_service as config_service_module
from app.services.config_service import (
    ConfigDraftSaveResult,
    ConfigService,
    FileConfigProvider,
    RuntimeConfigValidator,
    _classify_changed_fields,
)
from config import app_config_to_dict


def _payload(tmp_path: Path) -> dict:
    return {
        "telegram": {
            "token": "old-token",
            "whitelist_chat_ids": [1],
            "admlist_chat_ids": [1],
        },
        "tools": {},
        "defaults": {
            "workdir": str(tmp_path),
            "pending_input_confirmation_enabled": True,
        },
    }


def _write_config(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=False), encoding="utf-8")
    return path


def _revision(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _draft(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _service(path: Path) -> ConfigService:
    return ConfigService(FileConfigProvider(str(path)))


def _loaded_draft(service: ConfigService) -> dict:
    return app_config_to_dict(asyncio.run(service.load()))


def test_config_draft_save_result_contract() -> None:
    assert [field.name for field in fields(ConfigDraftSaveResult)] == [
        "ok",
        "revision",
        "diff",
        "changed",
        "restart_required",
        "reloadable",
        "errors",
        "backup_path",
        "not_applied",
        "secret_changed",
    ]

    legacy_result = ConfigDraftSaveResult(
        ok=True,
        revision="r1",
        diff="",
        changed=False,
        restart_required=[],
        reloadable=[],
        errors=[],
        backup_path=None,
    )
    assert legacy_result.not_applied == []
    assert legacy_result.secret_changed == []


def test_secret_changed_is_derived_from_classify_config_path(monkeypatch) -> None:
    calls: list[str] = []

    def classify(path: str):
        calls.append(path)
        return SimpleNamespace(apply_mode="hot_reload", secret=path == "custom.secret")

    monkeypatch.setattr(config_service_module, "classify_config_path", classify)

    restart_required, reloadable, not_applied, secret_changed = config_service_module._classify_changed_fields(
        {
            "custom.public": "old-public",
            "custom.secret": "old-secret",
        },
        {
            "custom.public": "new-public",
            "custom.secret": "new-secret",
        },
    )

    assert calls == ["custom.public", "custom.secret"]
    assert restart_required == []
    assert reloadable == ["custom.public", "custom.secret"]
    assert not_applied == []
    assert secret_changed == ["custom.secret"]


def test_runtime_config_validator_uses_canonical_config_model(tmp_path: Path) -> None:
    settings = RuntimeConfigValidator().validate_draft(_payload(tmp_path))

    assert settings.telegram.token == "old-token"
    assert settings.defaults.workdir == str(tmp_path)


def test_save_draft_with_revision_changed_writes_atomic_and_reports_backup(tmp_path: Path) -> None:
    path = _write_config(tmp_path, _payload(tmp_path))
    original_text = path.read_text(encoding="utf-8")
    expected_revision = _revision(path)
    service = _service(path)
    draft = _loaded_draft(service)
    draft["telegram"]["token"] = "new-token"
    draft["defaults"]["pending_input_confirmation_enabled"] = False

    result = asyncio.run(service.save_draft_with_revision(draft, expected_revision=expected_revision))

    assert result.ok is True
    assert result.changed is True
    assert result.errors == []
    assert result.revision == _revision(path)
    assert result.revision != expected_revision
    assert result.backup_path == f"{path}.bak"
    assert Path(result.backup_path).read_text(encoding="utf-8") == original_text
    assert "telegram.token" in result.restart_required
    assert "defaults.pending_input_confirmation_enabled" in result.reloadable
    assert result.not_applied == []
    assert result.secret_changed == ["telegram.token"]
    assert "new-token" in path.read_text(encoding="utf-8")
    assert not list(tmp_path.glob("cfg-*.tmp"))


def test_save_config_draft_with_revision_adapter_uses_app_config_contract(tmp_path: Path) -> None:
    path = _write_config(tmp_path, _payload(tmp_path))
    service = _service(path)
    loaded_config = asyncio.run(service.load())
    expected_revision = asyncio.run(service.current_revision(loaded_config))
    draft_config = copy.deepcopy(loaded_config)
    draft_config.telegram.token = "adapter-token"

    result = asyncio.run(
        service.save_config_draft_with_revision(
            draft_config,
            expected_revision=expected_revision,
        )
    )

    assert result.ok is True
    assert result.changed is True
    assert result.errors == []
    assert result.backup_path == f"{path}.bak"
    assert "telegram.token" in result.restart_required
    assert result.secret_changed == ["telegram.token"]
    assert "adapter-token" in path.read_text(encoding="utf-8")


def test_save_draft_with_revision_no_change_keeps_revision_and_skips_backup(tmp_path: Path) -> None:
    path = _write_config(tmp_path, _payload(tmp_path))
    expected_revision = _revision(path)
    service = _service(path)
    draft = _loaded_draft(service)

    result = asyncio.run(service.save_draft_with_revision(draft, expected_revision=expected_revision))

    assert result.ok is True
    assert result.changed is False
    assert result.diff == ""
    assert result.revision == expected_revision
    assert result.restart_required == []
    assert result.reloadable == []
    assert result.not_applied == []
    assert result.secret_changed == []
    assert result.errors == []
    assert result.backup_path is None
    assert not Path(f"{path}.bak").exists()


def test_save_draft_with_revision_rejects_revision_conflict_without_write(tmp_path: Path) -> None:
    path = _write_config(tmp_path, _payload(tmp_path))
    original_text = path.read_text(encoding="utf-8")
    current_revision = _revision(path)
    draft = _draft(path)
    draft["telegram"]["token"] = "new-token"

    result = asyncio.run(_service(path).save_draft_with_revision(draft, expected_revision="stale"))

    assert result.ok is False
    assert result.revision == current_revision
    assert result.changed is False
    assert result.errors == ["revision mismatch"]
    assert result.backup_path is None
    assert path.read_text(encoding="utf-8") == original_text


def test_save_draft_with_revision_returns_validation_errors_without_write(tmp_path: Path) -> None:
    path = _write_config(tmp_path, _payload(tmp_path))
    original_text = path.read_text(encoding="utf-8")
    current_revision = _revision(path)
    service = _service(path)
    draft = _loaded_draft(service)
    draft["telegram"]["token"] = ""

    result = asyncio.run(service.save_draft_with_revision(draft, expected_revision=current_revision))

    assert result.ok is False
    assert result.revision == current_revision
    assert result.changed is False
    assert result.diff == ""
    assert result.restart_required == []
    assert result.reloadable == []
    assert result.not_applied == []
    assert result.secret_changed == []
    assert result.backup_path is None
    assert any("telegram.token" in error for error in result.errors)
    assert path.read_text(encoding="utf-8") == original_text


def test_policy_derived_change_fields_include_not_applied_and_secret_changed() -> None:
    restart_required, reloadable, not_applied, secret_changed = _classify_changed_fields(
        {
            "telegram.token": "old-token",
            "tools.codex.prompt_regex": "old",
            "operator_file.local_only": "old",
            "defaults.pending_input_confirmation_enabled": True,
        },
        {
            "telegram.token": "new-token",
            "tools.codex.prompt_regex": "new",
            "operator_file.local_only": "new",
            "defaults.pending_input_confirmation_enabled": False,
        },
    )

    assert restart_required == ["telegram.token"]
    assert reloadable == [
        "defaults.pending_input_confirmation_enabled",
        "tools.codex.prompt_regex",
    ]
    assert not_applied == ["operator_file.local_only"]
    assert secret_changed == ["telegram.token"]
