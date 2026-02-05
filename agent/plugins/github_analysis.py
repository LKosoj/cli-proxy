from __future__ import annotations

import asyncio
import base64
import logging
import os
from typing import Any, Dict, List, Optional

import requests

from agent.plugins.base import ToolPlugin
from agent.tooling.spec import ToolSpec
from agent.tooling import helpers


class GitHubAnalysisTool(ToolPlugin):
    def get_source_name(self) -> str:
        return "GitHub"

    def get_spec(self) -> ToolSpec:
        return ToolSpec(
            name="github_analysis",
            description="Скачать содержимое файла(ов) из GitHub репозитория и вернуть текст. Можно указать путь к директории или файлу.",
            parameters={
                "type": "object",
                "properties": {
                    "owner": {"type": "string", "description": "Владелец репозитория"},
                    "repo": {"type": "string", "description": "Имя репозитория"},
                    "path": {"type": "string", "description": "Путь внутри репозитория (файл или директория)", "default": ""},
                    "max_files": {"type": "integer", "description": "Максимум файлов для скачивания", "default": 5},
                },
                "required": ["owner", "repo"],
            },
            parallelizable=True,
            timeout_ms=60_000,
        )

    async def execute(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        owner = (args.get("owner") or "").strip()
        repo = (args.get("repo") or "").strip()
        path = (args.get("path") or "").strip()
        max_files = int(args.get("max_files") or 5)
        max_files = max(1, min(max_files, 20))
        if not owner or not repo:
            return {"success": False, "error": "owner и repo обязательны"}

        token = os.getenv("GITHUB_TOKEN") or getattr(getattr(self, "config", None), "defaults", None) and getattr(self.config.defaults, "github_token", None)
        try:
            out = await asyncio.to_thread(self._fetch_sync, owner, repo, path, max_files, token)
        except Exception as e:
            logging.exception(f"tool failed {str(e)}")
            return {"success": False, "error": f"GitHub failed: {e}"}
        return {"success": True, "output": helpers._trim_output(out)}

    def _fetch_sync(self, owner: str, repo: str, path: str, max_files: int, token: Optional[str]) -> str:
        url = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}".rstrip("/")
        headers: Dict[str, str] = {"Accept": "application/vnd.github+json", "User-Agent": "cli-proxy/agent"}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        r = requests.get(url, headers=headers, timeout=30)
        if not r.ok:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
        data = r.json()
        items = data if isinstance(data, list) else [data]

        blocks: List[str] = []
        files_done = 0
        for it in items:
            if files_done >= max_files:
                break
            if not isinstance(it, dict):
                continue
            if it.get("type") != "file":
                continue
            name = it.get("path") or it.get("name") or ""
            content_b64 = it.get("content")
            enc = it.get("encoding")
            if content_b64 and enc == "base64":
                try:
                    raw = base64.b64decode(content_b64).decode("utf-8", errors="replace")
                except Exception:
                    raw = ""
            else:
                # Если контент не пришел сразу, дернем download_url.
                download = it.get("download_url")
                raw = ""
                if download:
                    rr = requests.get(download, headers={"User-Agent": "cli-proxy/agent"}, timeout=30)
                    if rr.ok:
                        raw = rr.text
            blocks.append(f"FILE: {name}\n{raw}".strip())
            files_done += 1

        if not blocks:
            return "Нет файлов для анализа (возможно, указан путь к директории без файлов или GitHub вернул не то)."
        return "\n\n".join(blocks)
