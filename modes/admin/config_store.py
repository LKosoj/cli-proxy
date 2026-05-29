from __future__ import annotations

import copy
import time
import logging
import os
import re
import shutil
import importlib.util
from typing import Any, Dict, Mapping, Optional

import yaml

try:
    from .schemas import validate_admin_config_payload
except ImportError:  # pragma: no cover - fallback for direct module loading in tests
    _SCHEMAS_PATH = os.path.join(os.path.dirname(__file__), "schemas.py")
    _SCHEMAS_SPEC = importlib.util.spec_from_file_location("modes_admin_schemas_direct", _SCHEMAS_PATH)
    if _SCHEMAS_SPEC is None or _SCHEMAS_SPEC.loader is None:
        raise
    _SCHEMAS_MODULE = importlib.util.module_from_spec(_SCHEMAS_SPEC)
    _SCHEMAS_SPEC.loader.exec_module(_SCHEMAS_MODULE)
    validate_admin_config_payload = _SCHEMAS_MODULE.validate_admin_config_payload
from utils.paths import cli_proxy_artifact_path

_log = logging.getLogger(__name__)
_CONFIG_FILE_NAME = "config.yaml"
_SECRETS_FILE_NAME = "secrets.env"
_ACTION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def _is_valid_action_id(action_id: str) -> bool:
    return bool(_ACTION_ID_RE.fullmatch(str(action_id or "").strip()))


class AdminConfigStoreError(RuntimeError):
    """Raised when admin config bootstrap/read fails."""


def default_admin_template_path() -> str:
    return os.path.join(os.path.dirname(__file__), "templates", _CONFIG_FILE_NAME)


def admin_config_path(session_workdir: str) -> str:
    workdir = str(session_workdir or "").strip()
    if not workdir:
        raise AdminConfigStoreError("session workdir is empty")
    workdir_abs = os.path.abspath(workdir)
    if not os.path.isdir(workdir_abs):
        raise AdminConfigStoreError(f"session workdir does not exist: {workdir_abs}")
    return cli_proxy_artifact_path(workdir_abs, f".admin/{_CONFIG_FILE_NAME}")


class AdminConfigStore:
    def __init__(self, session_workdir: str, *, template_path: Optional[str] = None) -> None:
        self.session_workdir = str(session_workdir or "").strip()
        self.template_path = os.path.abspath(str(template_path or default_admin_template_path()))

    @property
    def config_path(self) -> str:
        return admin_config_path(self.session_workdir)

    @property
    def secrets_path(self) -> str:
        workdir = str(self.session_workdir or "").strip()
        if not workdir:
            raise AdminConfigStoreError("session workdir is empty")
        return cli_proxy_artifact_path(os.path.abspath(workdir), f".admin/{_SECRETS_FILE_NAME}")

    def ensure_config(self) -> str:
        config_path = self.config_path
        config_dir = os.path.dirname(config_path)
        try:
            os.makedirs(config_dir, exist_ok=True)
        except Exception as exc:
            _log.exception("admin config: failed to create directory path=%s", config_dir)
            raise AdminConfigStoreError(f"failed to create admin config directory: {config_dir}") from exc

        if not os.path.exists(config_path):
            if not os.path.exists(self.template_path):
                raise AdminConfigStoreError(f"admin config template not found: {self.template_path}")
            try:
                shutil.copy2(self.template_path, config_path)
            except Exception as exc:
                _log.exception(
                    "admin config: failed to copy template src=%s dst=%s",
                    self.template_path,
                    config_path,
                )
                raise AdminConfigStoreError("failed to bootstrap admin config from template") from exc

        secrets_path = self.secrets_path
        if not os.path.exists(secrets_path):
            try:
                with open(secrets_path, "w", encoding="utf-8") as f:
                    f.write("# Admin mode local secrets\n")
            except Exception as exc:
                _log.exception("admin config: failed to create secrets file path=%s", secrets_path)
                raise AdminConfigStoreError("failed to bootstrap admin secrets file") from exc
        return config_path

    def load_config(self, *, validate: bool = True) -> Dict[str, Any]:
        config_path = self.config_path
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                payload = yaml.safe_load(f)
        except FileNotFoundError:
            return {}
        except yaml.YAMLError as exc:
            _log.exception("admin config: invalid YAML path=%s", config_path)
            raise AdminConfigStoreError(f"invalid YAML in admin config: {config_path}") from exc
        except Exception as exc:
            _log.exception("admin config: failed to read path=%s", config_path)
            raise AdminConfigStoreError(f"failed to read admin config: {config_path}") from exc

        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            raise AdminConfigStoreError(f"admin config must be a YAML mapping: {config_path}")
        if validate:
            self.validate_config(payload)
        return dict(payload)

    def load_effective_config(self, *, validate: bool = True) -> Dict[str, Any]:
        payload = self.load_config(validate=validate)
        return self.build_effective_config(payload)

    @classmethod
    def build_effective_config(cls, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(payload, dict):
            return {}
        effective = copy.deepcopy(payload)
        admin_cfg = effective.get("admin")
        if not isinstance(admin_cfg, dict):
            return effective
        generated_cfg = admin_cfg.get("generated", {})
        if not isinstance(generated_cfg, dict):
            generated_cfg = {}
        generated_cfg = cls._filter_generated_inventory_for_manual_monitor(admin_cfg, generated_cfg)

        effective_admin = dict(admin_cfg)
        for section in ("environment", "policies", "runbooks"):
            generated_section = generated_cfg.get(section, {})
            manual_section = effective_admin.get(section, {})
            if isinstance(generated_section, dict):
                if isinstance(manual_section, dict):
                    effective_admin[section] = cls._deep_merge_dicts(generated_section, manual_section)
                else:
                    effective_admin[section] = copy.deepcopy(generated_section)

        for section, list_key in (("diagnostics", "checks"), ("incidents", "rules")):
            generated_section = generated_cfg.get(section, {})
            manual_section = effective_admin.get(section, {})
            if not isinstance(generated_section, dict):
                generated_section = {}
            if not isinstance(manual_section, dict):
                manual_section = {}
            merged_section = cls._deep_merge_dicts(generated_section, manual_section)
            merged_section[list_key] = cls._merge_named_rows(
                cls._coerce_named_rows(generated_section.get(list_key, [])),
                cls._coerce_named_rows(manual_section.get(list_key, [])),
            )
            effective_admin[section] = merged_section

        manual_actions = effective_admin.get("actions", {})
        generated_actions = generated_cfg.get("actions", {})
        if isinstance(generated_actions, dict):
            generated_remediation = generated_actions.get("remediation", {})
            if isinstance(generated_remediation, dict):
                if isinstance(manual_actions, dict):
                    merged_actions = dict(manual_actions)
                    manual_remediation = merged_actions.get("remediation", {})
                    if isinstance(manual_remediation, dict):
                        merged_actions["remediation"] = cls._deep_merge_dicts(generated_remediation, manual_remediation)
                    else:
                        merged_actions["remediation"] = copy.deepcopy(generated_remediation)
                    manual_actions = merged_actions
                else:
                    manual_actions = {"remediation": copy.deepcopy(generated_remediation)}

        if isinstance(manual_actions, dict):
            merged_actions = dict(manual_actions)
            generated_targets = generated_actions.get("targets", {}) if isinstance(generated_actions, dict) else {}
            merged_actions = cls._merge_target_maps(generated_targets, merged_actions)
            effective_admin["actions"] = merged_actions

        generated_monitor = generated_cfg.get("monitor", {})
        manual_monitor = effective_admin.get("monitor", {})
        if isinstance(generated_monitor, dict):
            merged_monitor = dict(manual_monitor) if isinstance(manual_monitor, dict) else {}
            generated_servers = generated_monitor.get("servers", [])
            manual_servers = merged_monitor.get("servers", [])
            merged_monitor["servers"] = cls._merge_server_lists(generated_servers, manual_servers)
            for key, value in generated_monitor.items():
                if key == "servers":
                    continue
                if key not in merged_monitor:
                    merged_monitor[key] = copy.deepcopy(value)
            effective_admin["monitor"] = merged_monitor

        runtime_overlay = generated_cfg.get("runtime_overlay", {})
        if isinstance(runtime_overlay, dict):
            manual_servers_cfg = effective_admin.get("servers", {})
            overlay_servers_cfg = runtime_overlay.get("servers", {})
            effective_admin["servers"] = cls._merge_named_server_maps(overlay_servers_cfg, manual_servers_cfg)

            manual_monitor = effective_admin.get("monitor", {})
            overlay_monitor = runtime_overlay.get("monitor", {})
            if isinstance(overlay_monitor, dict):
                merged_monitor = dict(manual_monitor) if isinstance(manual_monitor, dict) else {}
                overlay_servers = overlay_monitor.get("servers", [])
                manual_servers = merged_monitor.get("servers", [])
                if isinstance(overlay_servers, list):
                    merged_monitor["servers"] = cls._merge_server_lists(overlay_servers, manual_servers)
                for key, value in overlay_monitor.items():
                    if key == "servers":
                        continue
                    if key not in merged_monitor:
                        merged_monitor[key] = copy.deepcopy(value)
                effective_admin["monitor"] = merged_monitor

            for target_section in ("actions", "allowlist"):
                overlay_targets = runtime_overlay.get(target_section, {})
                manual_targets = effective_admin.get(target_section, {})
                effective_admin[target_section] = cls._merge_target_maps(overlay_targets, manual_targets)

        effective["admin"] = effective_admin
        return effective

    @classmethod
    def _filter_generated_inventory_for_manual_monitor(
        cls,
        admin_cfg: Mapping[str, Any],
        generated_cfg: Dict[str, Any],
    ) -> Dict[str, Any]:
        generated_transport = cls._generated_inventory_transport(generated_cfg)
        if generated_transport not in {"local", "ssh"}:
            return generated_cfg

        manual_targets = cls._manual_monitor_targets(admin_cfg)
        if len(manual_targets) != 1:
            return generated_cfg
        if generated_transport in manual_targets:
            return generated_cfg
        return {}

    @staticmethod
    def _generated_inventory_transport(generated_cfg: Mapping[str, Any]) -> str:
        environment = generated_cfg.get("environment", {})
        if not isinstance(environment, Mapping):
            return ""
        transport = str(environment.get("transport") or "").strip().lower()
        return transport if transport in {"local", "ssh"} else ""

    @staticmethod
    def _manual_monitor_targets(admin_cfg: Mapping[str, Any]) -> set[str]:
        monitor_cfg = admin_cfg.get("monitor", {})
        if not isinstance(monitor_cfg, Mapping):
            return set()
        servers = monitor_cfg.get("servers", [])
        if not isinstance(servers, (list, tuple)):
            return set()

        targets: set[str] = set()
        for item in servers:
            if not isinstance(item, Mapping):
                continue
            target = str(item.get("target") or item.get("transport") or "").strip().lower()
            if target in {"local", "ssh"}:
                targets.add(target)
        return targets

    def merge_generated_config(self, generated_payload: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(generated_payload, dict):
            raise AdminConfigStoreError("admin.generated update must be a mapping")

        self.ensure_config()
        current_payload = self.load_config(validate=True)
        admin_cfg = current_payload.get("admin")
        if not isinstance(admin_cfg, dict):
            raise AdminConfigStoreError("admin config must contain `admin` mapping")

        current_generated = admin_cfg.get("generated", {})
        if current_generated is None:
            current_generated = {}
        if not isinstance(current_generated, dict):
            raise AdminConfigStoreError("admin.generated must be a mapping")

        updated_payload = dict(current_payload)
        updated_admin = dict(admin_cfg)
        updated_admin["generated"] = self._deep_merge_dicts(current_generated, generated_payload)
        updated_payload["admin"] = updated_admin

        self.validate_config(updated_payload)
        self._write_config(updated_payload)
        return updated_payload

    def replace_generated_config(self, generated_payload: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(generated_payload, dict):
            raise AdminConfigStoreError("admin.generated replacement must be a mapping")
        self.ensure_config()
        current_payload = self.load_config(validate=True)
        admin_cfg = current_payload.get("admin")
        if not isinstance(admin_cfg, dict):
            raise AdminConfigStoreError("admin config must contain `admin` mapping")

        current_generated = admin_cfg.get("generated", {})
        if not isinstance(current_generated, dict):
            current_generated = {}
        updated_payload = dict(current_payload)
        updated_admin = dict(admin_cfg)
        replacement = copy.deepcopy(generated_payload)
        preserved_keys = {"manual"}
        for key in preserved_keys:
            current_value = current_generated.get(key)
            if key not in replacement and isinstance(current_value, dict):
                replacement[key] = copy.deepcopy(current_value)
        updated_admin["generated"] = replacement
        updated_payload["admin"] = updated_admin

        self.validate_config(updated_payload)
        self._write_config(updated_payload)
        return updated_payload

    def apply_scan_result(
        self,
        *,
        generated_payload: Dict[str, Any],
        last_scan_at: float,
        scan_status: str = "ready",
        scan_error: Any = None,
    ) -> Dict[str, Any]:
        if not isinstance(generated_payload, dict):
            raise AdminConfigStoreError("admin.generated replacement must be a mapping")
        self.ensure_config()
        current_payload = self.load_config(validate=True)
        admin_cfg = current_payload.get("admin")
        if not isinstance(admin_cfg, dict):
            raise AdminConfigStoreError("admin config must contain `admin` mapping")

        current_generated = admin_cfg.get("generated", {})
        if not isinstance(current_generated, dict):
            current_generated = {}
        runtime_cfg = admin_cfg.get("runtime", {})
        if not isinstance(runtime_cfg, dict):
            runtime_cfg = {}

        replacement = copy.deepcopy(generated_payload)
        manual_cfg = current_generated.get("manual", {})
        if isinstance(manual_cfg, dict) and "manual" not in replacement:
            replacement["manual"] = copy.deepcopy(manual_cfg)

        updated_payload = dict(current_payload)
        updated_admin = dict(admin_cfg)
        updated_runtime = dict(runtime_cfg)
        updated_runtime["last_scan_at"] = float(last_scan_at)
        updated_runtime["scan_status"] = str(scan_status or "").strip() or "ready"
        updated_runtime["scan_error"] = str(scan_error).strip() if scan_error not in {None, ""} else None
        updated_admin["runtime"] = updated_runtime
        updated_admin["generated"] = replacement
        updated_payload["admin"] = updated_admin

        self.validate_config(updated_payload)
        self._write_config(updated_payload)
        return updated_payload

    def set_pinned_cli(self, cli_name: Mapping[str, Any] | str) -> Dict[str, Any]:
        if isinstance(cli_name, Mapping):
            pinned_cli = {str(k): copy.deepcopy(v) for k, v in cli_name.items()}
            name = str(pinned_cli.get("name") or "").strip()
            if name:
                pinned_cli["name"] = name
        else:
            name = str(cli_name or "").strip()
            pinned_cli = {"name": name} if name else {}
        if not pinned_cli:
            raise AdminConfigStoreError("admin.runtime.pinned_cli.name is empty")
        return self.update_runtime(
            pinned_cli=pinned_cli,
            initialized_at=float(time.time()),
        )

    def update_runtime(
        self,
        *,
        pinned_cli: Optional[Mapping[str, Any]] = None,
        pinned_executor_profile: Optional[str] = None,
        initialized_at: Optional[float] = None,
        last_scan_at: Optional[float] = None,
        scan_status: Optional[str] = None,
        scan_error: Any = None,
    ) -> Dict[str, Any]:
        self.ensure_config()
        current_payload = self.load_config(validate=True)
        admin_cfg = current_payload.get("admin")
        if not isinstance(admin_cfg, dict):
            raise AdminConfigStoreError("admin config must contain `admin` mapping")

        runtime_cfg = admin_cfg.get("runtime", {})
        if runtime_cfg is None:
            runtime_cfg = {}
        if not isinstance(runtime_cfg, dict):
            raise AdminConfigStoreError("admin.runtime must be a mapping")

        updated_payload = dict(current_payload)
        updated_admin = dict(admin_cfg)
        updated_runtime = dict(runtime_cfg)
        if pinned_cli is not None:
            updated_runtime["pinned_cli"] = copy.deepcopy(dict(pinned_cli))
        if pinned_executor_profile is not None:
            profile = str(pinned_executor_profile or "").strip()
            updated_runtime["pinned_executor_profile"] = profile or None
        if initialized_at is not None:
            updated_runtime["initialized_at"] = float(initialized_at)
        if last_scan_at is not None:
            updated_runtime["last_scan_at"] = float(last_scan_at)
        if scan_status is not None:
            updated_runtime["scan_status"] = str(scan_status or "").strip() or "not_started"
        updated_runtime["scan_error"] = str(scan_error).strip() if scan_error not in {None, ""} else None
        updated_admin["runtime"] = updated_runtime
        updated_payload["admin"] = updated_admin

        self.validate_config(updated_payload)
        self._write_config(updated_payload)
        return updated_payload

    def set_last_scan_at(self, scan_ts: float) -> Dict[str, Any]:
        try:
            scan_value = float(scan_ts)
        except Exception as exc:
            raise AdminConfigStoreError("admin.runtime.last_scan_at must be a number") from exc
        if scan_value <= 0:
            raise AdminConfigStoreError("admin.runtime.last_scan_at must be > 0")
        return self.update_runtime(last_scan_at=scan_value)

    def validate_config(self, payload: Dict[str, Any]) -> None:
        if not isinstance(payload, dict):
            raise AdminConfigStoreError("admin config root must be a mapping")
        admin_cfg = payload.get("admin")
        if not isinstance(admin_cfg, dict):
            raise AdminConfigStoreError("admin config must contain `admin` mapping")
        try:
            validate_admin_config_payload(payload)
        except ValueError as exc:
            raise AdminConfigStoreError(str(exc)) from exc

        allowlist_cfg = admin_cfg.get("allowlist", {})
        if not isinstance(allowlist_cfg, dict):
            raise AdminConfigStoreError("admin.allowlist must be a mapping")
        for target in ("local", "ssh"):
            target_allowlist = allowlist_cfg.get(target, {})
            if isinstance(target_allowlist, list):
                for item in target_allowlist:
                    action_id = str(item or "").strip()
                    if not _is_valid_action_id(action_id):
                        raise AdminConfigStoreError(f"admin.allowlist.{target}: invalid action_id `{action_id}`")
                continue
            if not isinstance(target_allowlist, dict):
                raise AdminConfigStoreError(f"admin.allowlist.{target} must be mapping or list")
            for action_id, action_payload in target_allowlist.items():
                action_key = str(action_id or "").strip()
                if not _is_valid_action_id(action_key):
                    raise AdminConfigStoreError(f"admin.allowlist.{target}: invalid action_id `{action_key}`")
                if action_payload is not None and not isinstance(action_payload, (dict, list, tuple)):
                    raise AdminConfigStoreError(
                        f"admin.allowlist.{target}.{action_key} must be mapping/list/tuple"
                    )

        servers_cfg = admin_cfg.get("servers", {})
        normalized_servers: list[Dict[str, Any]] = []
        if isinstance(servers_cfg, dict):
            for server_id, server_payload in servers_cfg.items():
                row = dict(server_payload or {}) if isinstance(server_payload, dict) else {}
                row.setdefault("id", str(server_id or ""))
                normalized_servers.append(row)
        elif isinstance(servers_cfg, list):
            for item in servers_cfg:
                if isinstance(item, dict):
                    normalized_servers.append(dict(item))
        elif servers_cfg is not None:
            raise AdminConfigStoreError("admin.servers must be mapping or list")

        for item in normalized_servers:
            server_id = str(item.get("id") or item.get("server_id") or "").strip()
            target = str(item.get("target") or "").strip().lower()
            if not server_id:
                raise AdminConfigStoreError("admin.servers: each server must define id")
            if target and target not in {"local", "ssh"}:
                raise AdminConfigStoreError(f"admin.servers.{server_id}: target must be local|ssh")
            port = item.get("port")
            if port not in (None, ""):
                try:
                    if int(port) <= 0:
                        raise ValueError("invalid port")
                except Exception as exc:
                    raise AdminConfigStoreError(f"admin.servers.{server_id}: invalid ssh port") from exc

        monitor_cfg = admin_cfg.get("monitor", {})
        if monitor_cfg is not None and not isinstance(monitor_cfg, dict):
            raise AdminConfigStoreError("admin.monitor must be a mapping")
        if isinstance(monitor_cfg, dict):
            interval = monitor_cfg.get("interval_sec", 30)
            try:
                if float(interval) <= 0:
                    raise ValueError("invalid interval")
            except Exception as exc:
                raise AdminConfigStoreError("admin.monitor.interval_sec must be > 0") from exc

        runtime_cfg = admin_cfg.get("runtime", {})
        if runtime_cfg is not None and not isinstance(runtime_cfg, dict):
            raise AdminConfigStoreError("admin.runtime must be a mapping")
        if isinstance(runtime_cfg, dict):
            scan_status = str(runtime_cfg.get("scan_status") or "not_started").strip().lower()
            if scan_status not in {"not_started", "initializing", "running", "ready", "failed"}:
                raise AdminConfigStoreError("admin.runtime.scan_status must be one of not_started|initializing|running|ready|failed")
            for field in ("initialized_at", "last_scan_at"):
                value = runtime_cfg.get(field)
                if value in {None, ""}:
                    continue
                try:
                    float(value)
                except Exception as exc:
                    raise AdminConfigStoreError(f"admin.runtime.{field} must be numeric") from exc

    def _write_config(self, payload: Dict[str, Any]) -> None:
        config_path = self.config_path
        temp_path = f"{config_path}.tmp"
        try:
            with open(temp_path, "w", encoding="utf-8") as f:
                yaml.safe_dump(payload, f, sort_keys=False)
            os.replace(temp_path, config_path)
        except Exception as exc:
            _log.exception("admin config: failed to persist path=%s", config_path)
            raise AdminConfigStoreError(f"failed to persist admin config: {config_path}") from exc
        finally:
            try:
                if os.path.exists(temp_path):
                    os.unlink(temp_path)
            except Exception:
                _log.exception("admin config: failed to cleanup temp file path=%s", temp_path)

    @staticmethod
    def _deep_merge_dicts(base: Dict[str, Any], incoming: Dict[str, Any]) -> Dict[str, Any]:
        merged = copy.deepcopy(base)
        for key, value in incoming.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = AdminConfigStore._deep_merge_dicts(merged[key], value)
            else:
                merged[key] = copy.deepcopy(value)
        return merged

    @staticmethod
    def _merge_server_lists(generated_servers: Any, manual_servers: Any) -> list[Dict[str, Any]]:
        merged: Dict[str, Dict[str, Any]] = {}
        for item in list(generated_servers or []):
            if not isinstance(item, dict):
                continue
            server_id = str(item.get("id") or item.get("server_id") or "").strip()
            if not server_id:
                continue
            merged[server_id] = copy.deepcopy(item)
        for item in list(manual_servers or []):
            if not isinstance(item, dict):
                continue
            server_id = str(item.get("id") or item.get("server_id") or "").strip()
            if not server_id:
                continue
            if server_id in merged:
                merged[server_id] = AdminConfigStore._deep_merge_dicts(merged[server_id], item)
            else:
                merged[server_id] = copy.deepcopy(item)
        return list(merged.values())

    @staticmethod
    def _merge_named_rows(generated_rows: Any, manual_rows: Any) -> list[Any]:
        merged: Dict[str, Any] = {}
        ordered: list[Any] = []

        def _row_key(item: Any) -> str:
            if not isinstance(item, dict):
                return ""
            for field in ("id", "action_id", "check_id", "service", "name"):
                token = str(item.get(field) or "").strip()
                if token:
                    return token
            return ""

        for item in list(generated_rows or []):
            key = _row_key(item)
            if key:
                merged[key] = copy.deepcopy(item)
                ordered.append(key)
            else:
                ordered.append(copy.deepcopy(item))

        for item in list(manual_rows or []):
            key = _row_key(item)
            if not key:
                ordered.append(copy.deepcopy(item))
                continue
            if key in merged and isinstance(merged.get(key), dict) and isinstance(item, dict):
                merged[key] = AdminConfigStore._deep_merge_dicts(merged[key], item)
                continue
            merged[key] = copy.deepcopy(item)
            ordered.append(key)

        result: list[Any] = []
        seen_keys: set[str] = set()
        for item in ordered:
            if isinstance(item, str):
                if item in seen_keys:
                    continue
                seen_keys.add(item)
                result.append(copy.deepcopy(merged[item]))
                continue
            result.append(item)
        return result

    @staticmethod
    def _coerce_named_rows(rows: Any) -> list[Any]:
        if isinstance(rows, list):
            return list(rows)
        if isinstance(rows, tuple):
            return list(rows)
        if isinstance(rows, Mapping):
            normalized: list[Any] = []
            for key, value in rows.items():
                if isinstance(value, dict):
                    row = copy.deepcopy(value)
                    if not any(str(row.get(field) or "").strip() for field in ("id", "action_id", "check_id", "service", "name")):
                        row["name"] = str(key)
                    normalized.append(row)
                else:
                    normalized.append(copy.deepcopy(value))
            return normalized
        return []

    @staticmethod
    def _merge_named_server_maps(generated_servers: Any, manual_servers: Any) -> Dict[str, Any]:
        merged: Dict[str, Any] = {}
        if isinstance(generated_servers, Mapping):
            for key, value in generated_servers.items():
                merged[str(key)] = copy.deepcopy(value)
        if isinstance(manual_servers, Mapping):
            for key, value in manual_servers.items():
                token = str(key)
                if token in merged and isinstance(merged[token], dict) and isinstance(value, dict):
                    merged[token] = AdminConfigStore._deep_merge_dicts(merged[token], value)
                else:
                    merged[token] = copy.deepcopy(value)
        return merged

    @staticmethod
    def _merge_target_maps(generated_targets: Any, manual_targets: Any) -> Dict[str, Any]:
        merged: Dict[str, Any] = {}
        for target in ("local", "ssh"):
            generated_map = generated_targets.get(target, {}) if isinstance(generated_targets, Mapping) else {}
            manual_map = manual_targets.get(target, {}) if isinstance(manual_targets, Mapping) else {}
            if isinstance(generated_map, dict) and isinstance(manual_map, dict):
                merged[target] = AdminConfigStore._deep_merge_dicts(generated_map, manual_map)
            elif isinstance(manual_map, dict):
                merged[target] = copy.deepcopy(manual_map)
            elif isinstance(generated_map, dict):
                merged[target] = copy.deepcopy(generated_map)
            else:
                merged[target] = {}
        if isinstance(manual_targets, Mapping):
            for key, value in manual_targets.items():
                token = str(key)
                if token not in merged:
                    merged[token] = copy.deepcopy(value)
        return merged
