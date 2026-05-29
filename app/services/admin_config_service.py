from __future__ import annotations

import copy
import hashlib
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional

import yaml


def _admin_config_store_symbols() -> tuple[Any, Any, Any]:
    from modes.admin.config_store import AdminConfigStore, AdminConfigStoreError, admin_config_path

    return AdminConfigStore, AdminConfigStoreError, admin_config_path


class AdminConfigServiceError(RuntimeError):
    status = 400

    def __init__(self, message: str, *, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.status = int(status if status is not None else self.status)


class AdminConfigSessionRequiredError(AdminConfigServiceError):
    status = 400


class AdminConfigSessionNotFoundError(AdminConfigServiceError):
    status = 404


class AdminConfigRevisionConflictError(AdminConfigServiceError):
    status = 409


@dataclass(frozen=True)
class AdminConfigService:
    app: Any

    def get_yaml(self, session_uid: str) -> Dict[str, Any]:
        _, AdminConfigStoreError, admin_config_path = _admin_config_store_symbols()
        store = self._store_for_session(session_uid)
        try:
            store.ensure_config()
        except AdminConfigStoreError as exc:
            raise AdminConfigServiceError(str(exc), status=500) from exc
        path = admin_config_path(store.session_workdir)
        try:
            with open(path, "r", encoding="utf-8") as handle:
                yaml_text = handle.read()
        except FileNotFoundError:
            yaml_text = ""
        except Exception as exc:
            raise AdminConfigServiceError("admin config read failed", status=500) from exc
        return {
            "config_path": path,
            "yaml": yaml_text,
            "revision": self._revision(path) if os.path.exists(path) else "",
        }

    def save_yaml(
        self,
        session_uid: str,
        yaml_text: str,
        expected_revision: Any = None,
    ) -> Dict[str, Any]:
        _, AdminConfigStoreError, admin_config_path = _admin_config_store_symbols()
        store = self._store_for_session(session_uid)
        try:
            store.ensure_config()
        except AdminConfigStoreError as exc:
            raise AdminConfigServiceError(str(exc), status=400) from exc
        path = admin_config_path(store.session_workdir)
        current_revision = self._revision(path) if os.path.exists(path) else ""
        if expected_revision and str(expected_revision) != current_revision:
            raise AdminConfigRevisionConflictError("revision mismatch")

        parsed = self._parse_yaml_mapping(yaml_text)
        try:
            store.validate_config(parsed)
            store._write_config(parsed)
        except AdminConfigStoreError as exc:
            raise AdminConfigServiceError(str(exc), status=400) from exc
        return {
            "config_path": path,
            "revision": self._revision(path) if os.path.exists(path) else "",
        }

    def get_monitor_servers(self, session_uid: str) -> Dict[str, Any]:
        _, AdminConfigStoreError, _ = _admin_config_store_symbols()
        store = self._store_for_session(session_uid)
        try:
            store.ensure_config()
            payload = store.load_config()
        except AdminConfigStoreError as exc:
            raise AdminConfigServiceError(str(exc), status=400) from exc
        return self._monitor_response(payload)

    def load_config(self, session_uid: str, *, effective: bool = True) -> Dict[str, Any]:
        _, AdminConfigStoreError, _ = _admin_config_store_symbols()
        store = self._store_for_session(session_uid)
        try:
            store.ensure_config()
            if effective:
                return store.load_effective_config()
            return store.load_config()
        except AdminConfigStoreError as exc:
            raise AdminConfigServiceError(str(exc), status=400) from exc

    def save_monitor_servers(self, session_uid: str, payload: Mapping[str, Any]) -> Dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise AdminConfigServiceError("payload must be an object", status=400)
        normalized = self._normalize_monitor_servers(payload.get("servers", []))

        _, AdminConfigStoreError, _ = _admin_config_store_symbols()
        store = self._store_for_session(session_uid)
        try:
            store.ensure_config()
            config_payload = store.load_config()
        except AdminConfigStoreError as exc:
            raise AdminConfigServiceError(str(exc), status=400) from exc

        admin_cfg = config_payload.get("admin") if isinstance(config_payload, dict) else None
        if not isinstance(admin_cfg, dict):
            raise AdminConfigServiceError("admin config is missing `admin` mapping", status=400)
        monitor_cfg = admin_cfg.get("monitor")
        if not isinstance(monitor_cfg, dict):
            monitor_cfg = {}
        else:
            monitor_cfg = copy.deepcopy(monitor_cfg)
        monitor_cfg["servers"] = normalized
        if "enabled" in payload:
            monitor_cfg["enabled"] = bool(payload.get("enabled"))
        interval_raw = payload.get("interval_sec")
        if interval_raw not in (None, ""):
            monitor_cfg["interval_sec"] = self._normalize_interval(interval_raw, field_name="interval_sec")

        updated_admin = dict(admin_cfg)
        updated_admin["monitor"] = monitor_cfg
        updated_payload = dict(config_payload)
        updated_payload["admin"] = updated_admin
        try:
            store.validate_config(updated_payload)
            store._write_config(updated_payload)
        except AdminConfigStoreError as exc:
            raise AdminConfigServiceError(str(exc), status=400) from exc
        return self._monitor_response(updated_payload)

    def get_ssh_actions(self, session_uid: str) -> Dict[str, Any]:
        payload = self.load_config(session_uid, effective=False)
        admin_cfg = payload.get("admin") if isinstance(payload, dict) else None
        actions_cfg = admin_cfg.get("actions") if isinstance(admin_cfg, dict) else None
        ssh_actions = actions_cfg.get("ssh") if isinstance(actions_cfg, dict) else None
        items: list[Dict[str, Any]] = []
        if isinstance(ssh_actions, dict):
            for action_id, action_payload in ssh_actions.items():
                if not isinstance(action_payload, dict):
                    continue
                argv = action_payload.get("argv")
                argv_list = [str(x) for x in argv] if isinstance(argv, (list, tuple)) else []
                timeout_raw = action_payload.get("timeout_sec")
                timeout_sec: Optional[float] = None
                if timeout_raw not in (None, ""):
                    try:
                        timeout_sec = float(timeout_raw)
                    except (TypeError, ValueError):
                        timeout_sec = None
                risk_level = str(action_payload.get("risk_level") or "").strip().lower() or "low"
                items.append({
                    "action_id": str(action_id),
                    "argv": argv_list,
                    "timeout_sec": timeout_sec,
                    "risk_level": risk_level,
                    "read_only": bool(action_payload.get("read_only")),
                    "description": str(action_payload.get("description") or ""),
                })
        return {"actions": items}

    def save_ssh_actions(self, session_uid: str, actions: list[Mapping[str, Any]]) -> Dict[str, Any]:
        action_re = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
        valid_risk = {"low", "medium", "high"}
        normalized: Dict[str, Any] = {}
        for item in actions or []:
            if not isinstance(item, Mapping):
                raise AdminConfigServiceError("action must be a mapping", status=400)
            action_id = str(item.get("action_id") or "").strip()
            if not action_id or not action_re.fullmatch(action_id):
                raise AdminConfigServiceError(f"invalid action_id: {action_id or '(empty)'}", status=400)
            argv = item.get("argv")
            if not isinstance(argv, list) or not argv:
                raise AdminConfigServiceError(f"action {action_id}: argv must be non-empty list", status=400)
            argv_list = [str(x) for x in argv if str(x).strip()]
            if not argv_list:
                raise AdminConfigServiceError(f"action {action_id}: argv items must be non-empty", status=400)
            timeout_raw = item.get("timeout_sec")
            try:
                timeout_sec = float(timeout_raw) if timeout_raw not in (None, "") else 30.0
            except (TypeError, ValueError) as exc:
                raise AdminConfigServiceError(
                    f"action {action_id}: timeout_sec must be numeric",
                    status=400,
                ) from exc
            if timeout_sec <= 0:
                raise AdminConfigServiceError(f"action {action_id}: timeout_sec must be > 0", status=400)
            risk_level = str(item.get("risk_level") or "low").strip().lower()
            if risk_level not in valid_risk:
                raise AdminConfigServiceError(
                    f"action {action_id}: risk_level must be low|medium|high",
                    status=400,
                )
            read_only = bool(item.get("read_only"))
            if read_only and risk_level != "low":
                raise AdminConfigServiceError(
                    f"action {action_id}: read_only actions must have risk_level=low",
                    status=400,
                )
            row: Dict[str, Any] = {
                "argv": argv_list,
                "timeout_sec": timeout_sec,
                "risk_level": risk_level,
            }
            if read_only:
                row["read_only"] = True
            description = str(item.get("description") or "").strip()
            if description:
                row["description"] = description
            normalized[action_id] = row

        _, AdminConfigStoreError, _ = _admin_config_store_symbols()
        store = self._store_for_session(session_uid)
        try:
            store.ensure_config()
            payload = store.load_config()
            admin_cfg = payload.get("admin") if isinstance(payload, dict) else None
            if not isinstance(admin_cfg, dict):
                raise AdminConfigServiceError("admin config is missing `admin` mapping", status=400)
            actions_cfg = admin_cfg.get("actions")
            if not isinstance(actions_cfg, dict):
                actions_cfg = {}
            actions_cfg["ssh"] = normalized
            admin_cfg["actions"] = actions_cfg
            payload["admin"] = admin_cfg
            store.validate_config(payload)
            store._write_config(payload)
        except AdminConfigServiceError:
            raise
        except AdminConfigStoreError as exc:
            raise AdminConfigServiceError(str(exc), status=400) from exc
        return {"actions": self.get_ssh_actions(session_uid)["actions"]}

    def _store_for_session(self, session_uid: str) -> Any:
        session = self._session_by_uid(session_uid)
        workdir = str(getattr(session, "workdir", "") or "").strip()
        if not workdir:
            raise AdminConfigServiceError("session workdir is not set", status=400)
        AdminConfigStore, _, _ = _admin_config_store_symbols()
        return AdminConfigStore(workdir)

    def _session_by_uid(self, session_uid: str) -> Any:
        token = str(session_uid or "").strip()
        if not token:
            raise AdminConfigSessionRequiredError("session_uid is required")
        manager = getattr(self.app, "manager", None)
        if manager is not None and hasattr(manager, "get_by_uid"):
            try:
                session = manager.get_by_uid(token)
            except Exception as exc:
                raise AdminConfigSessionNotFoundError("session not found") from exc
            if session is not None:
                return session
        session_service = getattr(self.app, "session_service", None)
        get_session_by_uid = getattr(session_service, "get_session_by_uid", None)
        if callable(get_session_by_uid):
            try:
                session = get_session_by_uid(token)
            except Exception as exc:
                raise AdminConfigSessionNotFoundError("session not found") from exc
            if session is not None:
                return session
        if manager is not None:
            for sessions in dict(getattr(manager, "sessions_by_chat", {}) or {}).values():
                if not isinstance(sessions, dict):
                    continue
                for session in sessions.values():
                    if str(getattr(session, "id", "") or "").strip() == token:
                        return session
        raise AdminConfigSessionNotFoundError("session not found")

    @staticmethod
    def _revision(path: str) -> str:
        with open(path, "rb") as handle:
            return hashlib.sha256(handle.read()).hexdigest()

    @staticmethod
    def _parse_yaml_mapping(yaml_text: str) -> Dict[str, Any]:
        try:
            parsed = yaml.safe_load(str(yaml_text or ""))
        except yaml.YAMLError as exc:
            raise AdminConfigServiceError(f"invalid YAML: {exc}", status=400) from exc
        if not isinstance(parsed, dict):
            raise AdminConfigServiceError("admin config must be a YAML mapping", status=400)
        return dict(parsed)

    @classmethod
    def _monitor_response(cls, payload: Dict[str, Any]) -> Dict[str, Any]:
        admin_cfg = payload.get("admin") if isinstance(payload, dict) else None
        monitor_cfg = admin_cfg.get("monitor") if isinstance(admin_cfg, dict) else None
        raw_servers = monitor_cfg.get("servers") if isinstance(monitor_cfg, dict) else None
        servers: list = []
        if isinstance(raw_servers, list):
            for item in raw_servers:
                if not isinstance(item, dict):
                    continue
                server_id = str(item.get("id") or item.get("server_id") or "").strip()
                target = str(item.get("target") or "").strip().lower()
                action_id = str(item.get("action_id") or "").strip()
                if not server_id or not action_id:
                    continue
                timeout_sec = None
                timeout_raw = item.get("timeout_sec")
                if timeout_raw not in (None, ""):
                    try:
                        timeout_sec = float(timeout_raw)
                    except (TypeError, ValueError):
                        timeout_sec = None
                servers.append({
                    "id": server_id,
                    "target": target or "local",
                    "action_id": action_id,
                    "timeout_sec": timeout_sec,
                })
        interval_raw = monitor_cfg.get("interval_sec") if isinstance(monitor_cfg, dict) else None
        try:
            interval_sec = float(interval_raw) if interval_raw not in (None, "") else 30.0
        except (TypeError, ValueError):
            interval_sec = 30.0
        enabled = True
        if isinstance(monitor_cfg, dict) and "enabled" in monitor_cfg:
            enabled = bool(monitor_cfg.get("enabled"))
        return {
            "servers": servers,
            "interval_sec": interval_sec,
            "enabled": enabled,
        }

    @classmethod
    def _normalize_monitor_servers(cls, raw_servers: Any) -> list[Dict[str, Any]]:
        if not isinstance(raw_servers, list):
            raise AdminConfigServiceError("servers must be a list", status=400)
        normalized: list[Dict[str, Any]] = []
        for item in raw_servers:
            if not isinstance(item, dict):
                raise AdminConfigServiceError("each server must be an object", status=400)
            server_id = str(item.get("id") or "").strip()
            target = str(item.get("target") or "").strip().lower()
            action_id = str(item.get("action_id") or "").strip()
            if not server_id or not action_id:
                raise AdminConfigServiceError("server id and action_id are required", status=400)
            if target not in ("local", "ssh"):
                raise AdminConfigServiceError(f"target must be local|ssh: {server_id}", status=400)
            row: Dict[str, Any] = {"id": server_id, "target": target, "action_id": action_id}
            timeout_raw = item.get("timeout_sec")
            if timeout_raw not in (None, ""):
                try:
                    timeout_sec = float(timeout_raw)
                except (TypeError, ValueError) as exc:
                    raise AdminConfigServiceError(
                        f"timeout_sec must be numeric: {server_id}",
                        status=400,
                    ) from exc
                if timeout_sec <= 0:
                    raise AdminConfigServiceError(f"timeout_sec must be > 0: {server_id}", status=400)
                row["timeout_sec"] = timeout_sec
            normalized.append(row)
        return normalized

    @staticmethod
    def _normalize_interval(value: Any, *, field_name: str) -> float:
        try:
            normalized = float(value)
        except (TypeError, ValueError) as exc:
            raise AdminConfigServiceError(f"{field_name} must be numeric", status=400) from exc
        if normalized <= 0:
            raise AdminConfigServiceError(f"{field_name} must be > 0", status=400)
        return normalized
