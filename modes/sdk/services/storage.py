from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict

from modes.sdk.json_store import read_json_locked, update_json_locked
from utils.paths import cli_proxy_artifact_path


def _default_state_path(root_dir: str, session_id: str) -> str:
    return os.path.join(cli_proxy_artifact_path(str(root_dir), ".modes"), "state", f"{session_id}.json")


@dataclass
class StorageService:
    """
    Namespaced state persistence.

    Data layout (single JSON file):
      {
        "modes": {
          "<mode_id>": {
            "<namespace>": { ... }
          }
        }
      }
    """

    path: str
    mode_id: str

    @classmethod
    def for_session(cls, *, root_dir: str, session_id: str, mode_id: str) -> StorageService:
        return cls(path=_default_state_path(root_dir, session_id), mode_id=mode_id)

    def _read(self) -> Dict[str, Any]:
        return read_json_locked(self.path, default={"modes": {}})

    def _update(self, updater):
        return update_json_locked(self.path, updater, default={"modes": {}})

    def get(self, key: str, default: Any = None, *, namespace: str = "default") -> Any:
        data = self._read()
        modes = data.get("modes") if isinstance(data, dict) else {}
        mode_state = (modes or {}).get(self.mode_id, {})
        ns = (mode_state or {}).get(namespace, {})
        if not isinstance(ns, dict):
            return default
        return ns.get(key, default)

    def set(self, key: str, value: Any, *, namespace: str = "default") -> None:
        def _u(current: Dict[str, Any]) -> Dict[str, Any]:
            current.setdefault("modes", {})
            modes = current["modes"]
            if not isinstance(modes, dict):
                modes = {}
                current["modes"] = modes
            mode_state = modes.setdefault(self.mode_id, {})
            if not isinstance(mode_state, dict):
                mode_state = {}
                modes[self.mode_id] = mode_state
            ns = mode_state.setdefault(namespace, {})
            if not isinstance(ns, dict):
                ns = {}
                mode_state[namespace] = ns
            ns[str(key)] = value
            return current

        self._update(_u)

    def delete(self, key: str, *, namespace: str = "default") -> bool:
        deleted = {"ok": False}

        def _u(current: Dict[str, Any]) -> Dict[str, Any]:
            current.setdefault("modes", {})
            modes = current.get("modes") or {}
            if not isinstance(modes, dict):
                return current
            mode_state = modes.get(self.mode_id) or {}
            if not isinstance(mode_state, dict):
                return current
            ns = mode_state.get(namespace) or {}
            if not isinstance(ns, dict):
                return current
            if str(key) in ns:
                ns.pop(str(key), None)
                deleted["ok"] = True
            return current

        self._update(_u)
        return bool(deleted["ok"])

    def get_namespace(self, *, namespace: str = "default") -> Dict[str, Any]:
        data = self._read()
        modes = data.get("modes") if isinstance(data, dict) else {}
        mode_state = (modes or {}).get(self.mode_id, {})
        ns = (mode_state or {}).get(namespace, {})
        return dict(ns) if isinstance(ns, dict) else {}

    def clear_namespace(self, *, namespace: str = "default") -> None:
        def _u(current: Dict[str, Any]) -> Dict[str, Any]:
            current.setdefault("modes", {})
            modes = current.get("modes") or {}
            if not isinstance(modes, dict):
                current["modes"] = {}
                modes = current["modes"]
            mode_state = modes.setdefault(self.mode_id, {})
            if not isinstance(mode_state, dict):
                mode_state = {}
                modes[self.mode_id] = mode_state
            mode_state[namespace] = {}
            return current

        self._update(_u)

    def list_namespaces(self) -> list[str]:
        data = self._read()
        modes = data.get("modes") if isinstance(data, dict) else {}
        mode_state = (modes or {}).get(self.mode_id, {})
        if not isinstance(mode_state, dict):
            return []
        return sorted([k for k in mode_state.keys() if isinstance(k, str)])
