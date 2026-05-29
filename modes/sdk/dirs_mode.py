from __future__ import annotations

from typing import Optional, Tuple


def encode_mode_dirs(mode_id: str, flow: str) -> str:
    return f"mode:{str(mode_id or '').strip()}:{str(flow or '').strip()}"


def decode_mode_dirs(value: str | None) -> Tuple[Optional[str], Optional[str]]:
    raw = str(value or "").strip()
    if not raw:
        return None, None
    if raw.startswith("mode:"):
        parts = raw.split(":", 2)
        if len(parts) >= 3:
            mode_id = str(parts[1] or "").strip() or None
            flow = str(parts[2] or "").strip() or None
            return mode_id, flow
    return None, None
