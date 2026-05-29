from __future__ import annotations

import copy
import difflib
import hashlib
import logging
import os
import shutil
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional

from pydantic import ValidationError

from app.config_runtime.adapter import adapt_validated_settings
from app.config_runtime.loader import ValidatedSettings
from app.config_runtime.serialization import dump_app_config_yaml
from app.services.config_apply_policy import classify_config_path
from config import AppConfig, app_config_to_dict, load_config


class ConfigProvider(ABC):
    """Абстракция источника конфигурации для async-сценариев."""

    @abstractmethod
    async def load(self) -> AppConfig:
        """Загружает полную конфигурацию приложения."""

    @abstractmethod
    async def get(self, key: str, default: Any = None) -> Any:
        """Возвращает значение по dotted-ключу."""


class FileConfigProvider(ConfigProvider):
    """Провайдер конфигурации из YAML-файла."""

    def __init__(self, path: str):
        self._path = str(path)
        self._cached: Optional[AppConfig] = None

    async def load(self) -> AppConfig:
        cfg = load_config(self._path)
        self._cached = cfg
        return cfg

    async def get(self, key: str, default: Any = None) -> Any:
        cfg = self._cached or await self.load()
        current: Any = cfg
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


class RuntimeConfigValidator:
    """Validates runtime config draft payloads against the canonical model."""

    def validate_draft(self, draft: dict[str, Any]) -> ValidatedSettings:
        return ValidatedSettings.model_validate(copy.deepcopy(draft))


@dataclass(frozen=True)
class AppRuntimeParams:
    config_path: str
    workdir: str
    state_path: str
    desktop_state_path: str
    toolhelp_path: str
    log_path: str


@dataclass(frozen=True)
class ConfigSaveResult:
    path: str
    backup_path: Optional[str]
    diff: str
    changed: bool


@dataclass(frozen=True)
class ConfigDraftSaveResult:
    ok: bool
    revision: str
    diff: str
    changed: bool
    restart_required: list[str]
    reloadable: list[str]
    errors: list[str]
    backup_path: Optional[str]
    not_applied: list[str] = field(default_factory=list)
    secret_changed: list[str] = field(default_factory=list)


def _file_revision(path: str) -> str:
    try:
        with open(path, "rb") as f:
            data = f.read()
    except FileNotFoundError:
        return "missing"
    return hashlib.sha256(data).hexdigest()


def _flatten_config(prefix: str, value: Any, out: dict[str, Any]) -> None:
    if isinstance(value, dict):
        for key in sorted(value.keys()):
            child = f"{prefix}.{key}" if prefix else str(key)
            _flatten_config(child, value[key], out)
        return
    out[prefix] = value


def _classify_changed_fields(
    flat_current: dict[str, Any],
    flat_draft: dict[str, Any],
) -> tuple[list[str], list[str], list[str], list[str]]:
    restart_required: list[str] = []
    reloadable: list[str] = []
    not_applied: list[str] = []
    secret_changed: list[str] = []
    for key in sorted(set(flat_current.keys()) | set(flat_draft.keys())):
        if flat_current.get(key) == flat_draft.get(key):
            continue
        policy = classify_config_path(key)
        if policy.secret:
            secret_changed.append(key)
        if policy.apply_mode == "restart_required":
            restart_required.append(key)
        elif policy.apply_mode == "hot_reload":
            reloadable.append(key)
        else:
            not_applied.append(key)
    return restart_required, reloadable, not_applied, secret_changed


class ConfigService:
    def __init__(
        self,
        provider: ConfigProvider,
        logger: Optional[logging.Logger] = None,
        validator: Optional[RuntimeConfigValidator] = None,
    ):
        self.provider = provider
        self.logger = logger or logging.getLogger(__name__)
        self.validator = validator or RuntimeConfigValidator()
        self._config: Optional[AppConfig] = None

    @property
    def config(self) -> Optional[AppConfig]:
        """Загруженная конфигурация (None до первого load())."""
        return self._config

    async def load(self) -> AppConfig:
        cfg = await self.provider.load()
        self._config = cfg
        return cfg

    async def get_value(self, key: str, default: Any = None) -> Any:
        return await self.provider.get(key, default)

    async def current_revision(self, config: Optional[AppConfig] = None) -> str:
        cfg = config or self._config or await self.load()
        return _file_revision(str(cfg.path))

    async def is_feature_enabled(self, flag_name: str) -> bool:
        """Централизованная проверка feature flags из defaults.*."""
        cfg = self._config or await self.load()
        token = str(flag_name or "").strip()
        if token.startswith("defaults."):
            token = token.split(".", 1)[1].strip()
        if not token:
            return False
        try:
            value = getattr(cfg.defaults, token)
        except AttributeError:
            self.logger.warning("unknown feature flag requested: %s", token)
            return False
        return bool(value)

    @staticmethod
    def _as_dict(config: AppConfig) -> dict[str, Any]:
        return app_config_to_dict(config)

    async def serialize_config(self, config: AppConfig) -> str:
        # Секреты сохраняются прямо в YAML (в открытом виде).
        # Запрещено использовать какие-либо placeholders.
        return dump_app_config_yaml(config)

    async def serialize_disk_config(self, path: str) -> str:
        if not os.path.exists(path):
            return ""
        return await self.serialize_config(load_config(path))

    async def diff_against_disk(self, config: AppConfig) -> str:
        path = str(config.path)
        before = await self.serialize_disk_config(path)
        after = await self.serialize_config(config)
        return self.generate_diff(before, after, path=path)

    @staticmethod
    def generate_diff(before: str, after: str, *, path: str = "config.yaml") -> str:
        lines = difflib.unified_diff(
            before.splitlines(),
            after.splitlines(),
            fromfile=f"{path}:before",
            tofile=f"{path}:after",
            lineterm="",
        )
        return "\n".join(lines)

    async def save_atomic(self, config: AppConfig, *, create_backup: bool = True) -> ConfigSaveResult:
        path = str(config.path)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        after = await self.serialize_config(config)
        diff = await self.diff_against_disk(config)
        changed = bool(diff.strip())

        if not changed:
            self._config = config
            self.logger.info(
                "config saved",
                extra={"action": "config_save", "path": path, "changed": False, "backup": False},
            )
            return ConfigSaveResult(path=path, backup_path=None, diff=diff, changed=False)

        backup_path: Optional[str] = None
        if create_backup and os.path.exists(path):
            backup_path = f"{path}.bak"
            shutil.copy2(path, backup_path)

        fd, tmp_path = tempfile.mkstemp(prefix="cfg-", suffix=".tmp", dir=os.path.dirname(path) or ".")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(after)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, path)
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    self.logger.exception("failed to remove temp config file path=%s", tmp_path)

        self._config = config
        self.logger.info(
            "config saved",
            extra={"action": "config_save", "path": path, "changed": changed, "backup": bool(backup_path)},
        )
        return ConfigSaveResult(path=path, backup_path=backup_path, diff=diff, changed=changed)

    async def save_draft_with_revision(
        self,
        draft: dict,
        *,
        expected_revision: str | None,
    ) -> ConfigDraftSaveResult:
        current_config = self._config or await self.load()
        path = str(current_config.path)
        current_revision = _file_revision(path)

        if expected_revision and expected_revision != current_revision:
            return ConfigDraftSaveResult(
                ok=False,
                revision=current_revision,
                diff="",
                changed=False,
                restart_required=[],
                reloadable=[],
                errors=["revision mismatch"],
                backup_path=None,
            )

        try:
            settings = self.validator.validate_draft(draft)
        except ValidationError as exc:
            return ConfigDraftSaveResult(
                ok=False,
                revision=current_revision,
                diff="",
                changed=False,
                restart_required=[],
                reloadable=[],
                errors=self._format_validation_errors(exc),
                backup_path=None,
            )
        except Exception as exc:
            self.logger.exception("config draft validation failed")
            return ConfigDraftSaveResult(
                ok=False,
                revision=current_revision,
                diff="",
                changed=False,
                restart_required=[],
                reloadable=[],
                errors=[str(exc)],
                backup_path=None,
            )

        draft_config = adapt_validated_settings(settings, path=path)
        diff = await self.diff_against_disk(draft_config)
        changed = bool(diff.strip())
        restart_required, reloadable, not_applied, secret_changed = self._draft_change_fields(current_config, draft_config)
        save_result = await self.save_atomic(draft_config)

        return ConfigDraftSaveResult(
            ok=True,
            revision=_file_revision(path),
            diff=diff,
            changed=changed,
            restart_required=restart_required,
            reloadable=reloadable,
            not_applied=not_applied,
            secret_changed=secret_changed,
            errors=[],
            backup_path=save_result.backup_path,
        )

    async def save_config_draft_with_revision(
        self,
        config: AppConfig,
        *,
        expected_revision: str | None,
    ) -> ConfigDraftSaveResult:
        return await self.save_draft_with_revision(
            self._as_dict(config),
            expected_revision=expected_revision,
        )

    @staticmethod
    def _format_validation_errors(exc: ValidationError) -> list[str]:
        errors: list[str] = []
        for error in exc.errors():
            location = ".".join(str(part) for part in error.get("loc", ())) or "<root>"
            errors.append(f"{location}: {error.get('msg', 'validation error')}")
        return errors

    @classmethod
    def _draft_change_fields(
        cls,
        current_config: AppConfig,
        draft_config: AppConfig,
    ) -> tuple[list[str], list[str], list[str], list[str]]:
        flat_current: dict[str, Any] = {}
        flat_draft: dict[str, Any] = {}
        _flatten_config("", cls._as_dict(current_config), flat_current)
        _flatten_config("", cls._as_dict(draft_config), flat_draft)
        return _classify_changed_fields(flat_current, flat_draft)

    async def resolve_runtime_params(self, config: Optional[AppConfig] = None) -> AppRuntimeParams:
        cfg = config or self._config or await self.load()
        workdir = os.path.abspath(os.path.expanduser(str(cfg.defaults.workdir or os.getcwd())))
        state_path = self._resolve_path(str(cfg.defaults.state_path), workdir)
        # desktop_state.json — в каталоге запуска (cwd), не в workdir
        launch_dir = os.getcwd()
        desktop_state_path = self._resolve_path(str(cfg.defaults.desktop_state_path), launch_dir)
        toolhelp_path = self._resolve_path(str(cfg.defaults.toolhelp_path), workdir)
        log_path = self._resolve_path(str(cfg.defaults.log_path), workdir)

        os.makedirs(workdir, exist_ok=True)
        for p in (state_path, desktop_state_path, toolhelp_path, log_path):
            os.makedirs(os.path.dirname(p) or ".", exist_ok=True)

        return AppRuntimeParams(
            config_path=os.path.abspath(str(cfg.path)),
            workdir=workdir,
            state_path=state_path,
            desktop_state_path=desktop_state_path,
            toolhelp_path=toolhelp_path,
            log_path=log_path,
        )

    async def validate_required_secrets(self, config: Optional[AppConfig] = None) -> list[str]:
        cfg = config or self._config or await self.load()
        missing: list[str] = []
        if not str(getattr(cfg.telegram, "token", "") or "").strip():
            missing.append("telegram.token")
        return missing

    @staticmethod
    def _resolve_path(path: str, base: str) -> str:
        raw = os.path.expanduser(str(path or "").strip())
        if not raw:
            return os.path.abspath(base)
        if os.path.isabs(raw):
            return os.path.abspath(raw)
        return os.path.abspath(os.path.join(base, raw))
