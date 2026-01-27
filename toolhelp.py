import json
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass
class ToolHelpEntry:
    tool: str
    content: str
    updated_at: float


def load_toolhelp(path: str) -> Dict[str, ToolHelpEntry]:
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        content = f.read().strip()
        if not content:
            return {}
        raw = json.loads(content)
    result: Dict[str, ToolHelpEntry] = {}
    for key, val in raw.items():
        result[key] = ToolHelpEntry(
            tool=val.get("tool", ""),
            content=val.get("content", ""),
            updated_at=float(val.get("updated_at", 0)),
        )
    return result


def save_toolhelp(path: str, data: Dict[str, ToolHelpEntry]) -> None:
    raw: Dict[str, Any] = {}
    for key, val in data.items():
        raw[key] = {
            "tool": val.tool,
            "content": val.content,
            "updated_at": val.updated_at,
        }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=2)


def update_toolhelp(path: str, tool: str, content: str) -> None:
    data = load_toolhelp(path)
    data[tool] = ToolHelpEntry(tool=tool, content=content, updated_at=time.time())
    save_toolhelp(path, data)


def get_toolhelp(path: str, tool: str) -> Optional[ToolHelpEntry]:
    data = load_toolhelp(path)
    return data.get(tool)
