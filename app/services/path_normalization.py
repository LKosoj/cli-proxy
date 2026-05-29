from __future__ import annotations

import os
from typing import Any, Optional


def _is_mock_path_value(path: Any) -> bool:
    module_name = str(getattr(type(path), "__module__", "") or "")
    return module_name.startswith("unittest.mock")


def _coerce_path_text(path: Any, *, field_name: str) -> str:
    if path is None:
        return ""
    if isinstance(path, bytes):
        return os.fsdecode(path)
    if isinstance(path, str):
        return path
    if _is_mock_path_value(path):
        raise TypeError(f"{field_name} must be str or os.PathLike, got {type(path).__name__}")
    fspath = getattr(type(path), "__fspath__", None)
    if callable(fspath):
        return os.fsdecode(os.fspath(path))
    raise TypeError(f"{field_name} must be str or os.PathLike, got {type(path).__name__}")


def normalize_optional_path(path: Any, *, field_name: str = "path") -> Optional[str]:
    token = _coerce_path_text(path, field_name=field_name).strip()
    if not token:
        return None
    return os.path.abspath(token)


def normalize_path(path: Any, *, field_name: str = "path", default_filename: str) -> str:
    normalized = normalize_optional_path(path, field_name=field_name)
    if normalized:
        return normalized
    return os.path.abspath(default_filename)


def normalize_optional_state_path(path: Any) -> Optional[str]:
    return normalize_optional_path(path, field_name="state_path")


def normalize_state_path(path: Any, *, default_filename: str = "state.json") -> str:
    return normalize_path(path, field_name="state_path", default_filename=default_filename)
