from __future__ import annotations

import logging
from collections.abc import Iterator, MutableMapping
from typing import Any, Callable, Dict

from app.services.path_normalization import normalize_optional_state_path
from app.services.state_repository import get_state_repository


_MISSING = object()


class PersistentStateMap(MutableMapping[str, Any]):
    """MutableMapping persisted inside the shared state JSON under a top-level key."""

    def __init__(self, state_path: str, root_key: str) -> None:
        self._log = logging.getLogger(__name__)
        self._fallback: Dict[str, Any] = {}
        try:
            self._state_path = normalize_optional_state_path(state_path) or ""
        except TypeError:
            self._log.warning("persistent state map disabled: invalid state_path type=%s", type(state_path).__name__)
            self._state_path = ""
        self._root_key = str(root_key or "").strip()
        self._repo = get_state_repository(self._state_path) if self._state_path else None

    def _read_bucket(self) -> Dict[str, Any]:
        if not self._state_path or not self._root_key:
            return dict(self._fallback)
        try:
            if self._repo is None:
                return dict(self._fallback)
            return self._repo.read_namespace(self._root_key)
        except Exception:
            self._log.exception("persistent state read failed key=%s", self._root_key)
            return dict(self._fallback)

    def _update_bucket(self, updater: Callable[[Dict[str, Any]], Dict[str, Any]]) -> None:
        if not self._state_path or not self._root_key:
            self._fallback = dict(updater(dict(self._fallback)))
            return

        def _apply(raw: Dict[str, Any]) -> Dict[str, Any]:
            next_bucket = updater(dict(raw))
            if not isinstance(next_bucket, dict):
                return dict(raw)
            return next_bucket

        try:
            if self._repo is None:
                return
            self._repo.update_namespace(self._root_key, _apply)
        except Exception:
            self._log.exception("persistent state update failed key=%s", self._root_key)
            self._fallback = dict(updater(dict(self._fallback)))

    def __getitem__(self, key: str) -> Any:
        skey = str(key)
        bucket = self._read_bucket()
        if skey not in bucket:
            raise KeyError(skey)
        return bucket[skey]

    def __setitem__(self, key: str, value: Any) -> None:
        skey = str(key)

        def _set(bucket: Dict[str, Any]) -> Dict[str, Any]:
            bucket[skey] = value
            return bucket

        self._update_bucket(_set)

    def __delitem__(self, key: str) -> None:
        skey = str(key)
        removed = {"ok": False}

        def _delete(bucket: Dict[str, Any]) -> Dict[str, Any]:
            if skey in bucket:
                removed["ok"] = True
                bucket.pop(skey, None)
            return bucket

        self._update_bucket(_delete)
        if not removed["ok"]:
            raise KeyError(skey)

    def __iter__(self) -> Iterator[str]:
        return iter(self._read_bucket())

    def __len__(self) -> int:
        return len(self._read_bucket())

    def get(self, key: str, default: Any = None) -> Any:
        return self._read_bucket().get(str(key), default)

    def pop(self, key: str, default: Any = _MISSING) -> Any:
        skey = str(key)
        popped = {"found": False, "value": None}

        def _pop(bucket: Dict[str, Any]) -> Dict[str, Any]:
            if skey in bucket:
                popped["found"] = True
                popped["value"] = bucket.pop(skey, None)
            return bucket

        self._update_bucket(_pop)
        if popped["found"]:
            return popped["value"]
        if default is _MISSING:
            raise KeyError(skey)
        return default

    def clear(self) -> None:
        self._update_bucket(lambda _bucket: {})
