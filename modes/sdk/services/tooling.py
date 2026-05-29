from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterable, Optional

from modes.sdk.runtime.json_normalizer import loads_safe


ExecuteToolFn = Callable[[str, dict, dict], Awaitable[dict]]
RegistryProviderFn = Callable[[], Any]


@dataclass
class ModeToolingService:
    execute_tool_fn: Optional[ExecuteToolFn] = None
    registry_provider: Optional[RegistryProviderFn] = None

    def get_registry(self) -> Any:
        if not self.registry_provider:
            raise RuntimeError("ModeToolingService.registry_provider is not configured")
        registry = self.registry_provider()
        if registry is None:
            raise RuntimeError("Tool registry is not configured")
        return registry

    async def execute(self, tool_name: str, args: dict, ctx: dict) -> dict:
        if not self.execute_tool_fn:
            raise RuntimeError("ModeToolingService.execute_tool_fn is not configured")
        response = await self.execute_tool_fn(str(tool_name or ""), dict(args or {}), dict(ctx or {}))
        if not isinstance(response, dict):
            raise RuntimeError(f"Tool {tool_name} returned invalid response type")
        return response

    async def ask_user(
        self,
        *,
        question: str,
        options: Iterable[str],
        ctx: dict,
        allow_custom: bool = False,
        system_options: bool = False,
    ) -> str:
        normalized_options = [str(x).strip() for x in (options or []) if str(x).strip()]
        response = await self.execute(
            "ask_user",
            {
                "question": str(question or ""),
                "options": list(normalized_options),
                "allow_custom": bool(allow_custom),
                "system_options": bool(system_options),
            },
            dict(ctx or {}),
        )
        if not bool(response.get("success")):
            raise RuntimeError(f"ask_user failed: {response.get('error')}")
        return self.parse_selected_option(
            response.get("output"),
            allowed_options=normalized_options,
            allow_custom=bool(allow_custom),
        )

    def parse_selected_option(
        self,
        output: Any,
        *,
        allowed_options: Optional[Iterable[str]] = None,
        allow_custom: bool = False,
    ) -> str:
        candidate = self._extract_candidate(output)
        normalized = self._normalize_text(candidate)
        if not normalized:
            raise RuntimeError("ask_user returned empty selection")

        allowed = [self._normalize_text(x) for x in (allowed_options or []) if self._normalize_text(x)]
        if not allowed:
            return normalized

        allowed_map = {value.casefold(): value for value in allowed}
        exact = allowed_map.get(normalized.casefold())
        if exact:
            return exact

        if bool(allow_custom):
            return normalized

        raise ValueError(f"ask_user returned invalid selection: {normalized}")

    def _extract_candidate(self, output: Any) -> str:
        if isinstance(output, dict):
            for key in ("selected_option", "selection", "value", "answer", "output"):
                value = output.get(key)
                if value is not None:
                    return str(value)
            return ""

        raw = str(output or "").strip()
        if not raw:
            return ""

        try:
            parsed = loads_safe(raw, strict_first=False)
        except Exception:
            parsed = None

        if isinstance(parsed, dict):
            for key in ("selected_option", "selection", "value", "answer", "output"):
                value = parsed.get(key)
                if value is not None:
                    return str(value)

        lowered = raw.casefold()
        prefixes = (
            "user selected:",
            "selected:",
            "selection:",
            "answer:",
        )
        for prefix in prefixes:
            if lowered.startswith(prefix):
                return raw[len(prefix):].strip()

        if ":" in raw:
            left, right = raw.split(":", 1)
            left_norm = left.strip().casefold()
            if left_norm in {"user selected", "selected", "selection", "answer"}:
                return right.strip()

        return raw

    @staticmethod
    def _normalize_text(value: str) -> str:
        text = html.unescape(str(value or "").strip())
        text = re.sub(r"<[^>]+>", "", text)
        text = text.strip()
        if len(text) >= 2 and text[0] == text[-1] and text[0] in {"\"", "'", "`"}:
            text = text[1:-1].strip()
        return text
