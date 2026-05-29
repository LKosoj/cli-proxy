from __future__ import annotations

from typing import Any


DESKTOP_ACTOR_ID = "desktop:default"


def normalize_actor_id(value: Any, *, default_surface: str = "telegram") -> str:
    if isinstance(value, str):
        token = str(value or "").strip()
        if not token:
            return ""
        if ":" in token:
            return token
        if token.lstrip("-").isdigit():
            return f"{default_surface}:{int(token)}"
        return f"{default_surface}:{token}"
    if isinstance(value, bool):
        return f"{default_surface}:{int(value)}"
    if isinstance(value, int):
        return f"{default_surface}:{int(value)}"
    token = str(value or "").strip()
    if not token:
        return ""
    if ":" in token:
        return token
    return f"{default_surface}:{token}"


def telegram_actor_id(value: Any) -> str:
    return normalize_actor_id(value, default_surface="telegram")


def miniapp_actor_id(value: Any) -> str:
    return telegram_actor_id(value)


def desktop_actor_id(value: Any = "default") -> str:
    token = str(value or "").strip() or "default"
    if ":" in token:
        return normalize_actor_id(token, default_surface="desktop")
    return f"desktop:{token}"


def internal_actor_id(value: Any = "system") -> str:
    token = str(value or "").strip() or "system"
    if ":" in token:
        return normalize_actor_id(token, default_surface="internal")
    return f"internal:{token}"


__all__ = [
    "DESKTOP_ACTOR_ID",
    "desktop_actor_id",
    "internal_actor_id",
    "miniapp_actor_id",
    "normalize_actor_id",
    "telegram_actor_id",
]
