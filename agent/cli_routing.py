from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

from app.services.cli_dialog_logger import log_cli_dialog
from app.services.task_bearing_cli_hook_service import get_task_bearing_cli_hook_service
from app.services.tool_availability import is_tool_available
from config import AppConfig
from modes.sdk.runtime.cli_contracts import wrap_prompt_for_response_format
from modes.sdk.runtime.json_normalizer import parse_normalize_validate
from session import Session

_log = logging.getLogger(__name__)


# Hardcoded defaults (used when config.defaults.cli_routing is missing/invalid).
DEFAULT_CLI_ROUTING: Dict[str, List[str]] = {
    "analytics": ["gemini", "claude", "qwen", "codex", "grok", "kimi"],
    "planning": ["gemini", "claude", "qwen", "codex", "grok", "kimi"],
    # Aggregate development (preferred when backend/frontend split is ambiguous).
    "development": ["claude", "codex", "qwen", "gemini", "grok", "kimi"],
    # Optional split types for higher routing precision.
    "backend_dev": ["claude", "codex", "qwen", "gemini", "grok", "kimi"],
    "frontend_dev": ["gemini", "claude", "qwen", "codex", "grok", "kimi"],
    "administration": ["qwen", "gemini", "codex", "claude", "grok", "kimi"],
    "website_administration": ["codex", "gemini", "claude", "qwen", "grok", "kimi"],
    "default": ["claude", "codex", "gemini", "qwen", "grok", "kimi"],
}

_WORK_TYPE_CLASSIFIER_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "required": ["work_type"],
    "properties": {
        "work_type": {"type": "string"},
        "confidence": {"type": "number"},
        "reason": {"type": "string"},
    },
    "additionalProperties": True,
}


def _looks_like_structured_payload(raw: str) -> bool:
    s = str(raw or "").strip()
    if not s:
        return False
    return s.startswith("{") or s.startswith("```")


async def _normalize_work_type(work_type: Any, config: AppConfig) -> str:
    _ = config
    raw = str(work_type or "").strip()
    if not raw:
        return "default"
    if not _looks_like_structured_payload(raw):
        return raw
    try:
        parsed = parse_normalize_validate(raw, _WORK_TYPE_CLASSIFIER_SCHEMA)
    except Exception:
        _log.exception("cli_routing work_type structured parse failed raw=%r", raw[:300])
        return "default"
    normalized = str(parsed.get("work_type") or "").strip()
    return normalized or "default"


def _load_routing_from_config(config: AppConfig) -> Dict[str, List[str]]:
    raw = getattr(getattr(config, "defaults", None), "cli_routing", None)
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, List[str]] = {}
    for k, v in raw.items():
        if not isinstance(k, str):
            continue
        if isinstance(v, list):
            out[k] = [str(x).strip() for x in v if str(x).strip()]
        elif isinstance(v, str):
            # Allow a single string for convenience.
            s = v.strip()
            out[k] = [s] if s else []
    return out


def get_priority_list(config: AppConfig, work_type: str) -> List[str]:
    wt = str(work_type or "").strip()
    cfg = _load_routing_from_config(config)
    if wt in cfg and cfg[wt]:
        return list(cfg[wt])
    if wt in DEFAULT_CLI_ROUTING:
        return list(DEFAULT_CLI_ROUTING[wt])
    # Unknown type: fall back to default.
    if "default" in cfg and cfg["default"]:
        return list(cfg["default"])
    return list(DEFAULT_CLI_ROUTING["default"])


def _filter_configured_tools(config: AppConfig, names: Iterable[str]) -> List[str]:
    configured = set((config.tools or {}).keys())
    out: List[str] = []
    for n in names:
        s = str(n or "").strip()
        if not s:
            continue
        if s not in configured:
            _log.warning("cli_routing: tool %r is not configured under tools.* (ignored)", s)
            continue
        out.append(s)
    return out


def pick_candidates(config: AppConfig, work_type: str) -> List[str]:
    primary = _filter_configured_tools(config, get_priority_list(config, work_type))
    fallback = _filter_configured_tools(config, get_priority_list(config, "default"))
    seen = set()
    merged: List[str] = []
    for n in primary + fallback:
        if n in seen:
            continue
        seen.add(n)
        merged.append(n)
    return merged


@dataclass
class RoutedCallError(Exception):
    work_type: str
    tried: List[Tuple[str, str]]

    def __str__(self) -> str:
        parts = [f"{cli}: {err}" for cli, err in (self.tried or [])]
        details = "; ".join(parts) if parts else "(no attempts)"
        return f"no available CLI succeeded for work_type={self.work_type}: {details}"


@contextlib.contextmanager
def temporary_session_cli(session: Session, cli_name: str):
    """
    Temporarily switch session.tool/active_cli for a single call and restore afterwards.
    This MUST NOT persist changes to the user's chosen active CLI.
    """
    prev_cli = str(
        getattr(getattr(session, "cli", None), "active_cli", "")
        or getattr(session, "active_cli", "")
        or getattr(getattr(session, "tool", None), "name", "")
        or ""
    )
    if not prev_cli:
        raise RuntimeError("session.cli.active_cli is not set")
    try:
        session.set_active_cli(cli_name)
        yield
    finally:
        try:
            session.set_active_cli(prev_cli)
        except Exception:
            # Restoration must not raise; log and continue.
            _log.exception("failed to restore previous session cli")


async def run_prompt_routed(
    session: Session,
    config: AppConfig,
    work_type: str,
    prompt: str,
    *,
    response_format: str = "",
    timeout_sec: Optional[int] = None,
    force_fresh: bool = False,
    chat_id: Optional[int] = None,
    task_bearing: bool = True,
    technical_command: Optional[bool] = None,
) -> str:
    """
    Run a prompt via the first available CLI by priority for the given work type.
    If the chosen CLI errors, tries the next candidate (failover) until one succeeds.
    """
    _, out = await run_prompt_routed_meta(
        session,
        config,
        work_type,
        prompt,
        response_format=response_format,
        timeout_sec=timeout_sec,
        force_fresh=force_fresh,
        chat_id=chat_id,
        task_bearing=task_bearing,
        technical_command=technical_command,
    )
    return out


async def run_prompt_routed_meta(
    session: Session,
    config: AppConfig,
    work_type: str,
    prompt: str,
    *,
    response_format: str = "",
    timeout_sec: Optional[int] = None,
    force_fresh: bool = False,
    chat_id: Optional[int] = None,
    task_bearing: bool = True,
    technical_command: Optional[bool] = None,
) -> Tuple[str, str]:
    """
    Same as run_prompt_routed(), but returns the CLI name that succeeded.
    """
    prompt_for_cli = wrap_prompt_for_response_format(prompt, str(response_format or "").strip().lower())
    normalized_work_type = await _normalize_work_type(work_type, config)
    candidates = pick_candidates(config, normalized_work_type)
    tried: List[Tuple[str, str]] = []
    hook = get_task_bearing_cli_hook_service(config)
    prepared = await hook.prepare_prompt(
        session=session,
        prompt=prompt_for_cli,
        source="cli_routing",
        phase="execute",
        task_bearing=task_bearing,
        technical_command=technical_command,
    )

    for cli in candidates:
        if not is_tool_available(config, cli):
            continue
        try:
            with temporary_session_cli(session, cli):
                if timeout_sec is not None and timeout_sec > 0:
                    import asyncio

                    out = await asyncio.wait_for(
                        session.run_prompt(prepared.prompt_for_cli, force_fresh=force_fresh),
                        timeout=timeout_sec,
                    )
                else:
                    out = await session.run_prompt(prepared.prompt_for_cli, force_fresh=force_fresh)
                log_cli_dialog(session, prompt, out, chat_id=chat_id)
                hook.record_success(prepared, output=out)
                return cli, out
        except Exception as e:
            tried.append((cli, str(e)))
            hook.record_retry(prepared, reason=f"{cli}: {e}")
            # Best-effort interrupt between attempts (headless mode / interactive may differ).
            try:
                session.interrupt()
            except Exception:
                pass
            continue

    hook.record_error(prepared, error=str(RoutedCallError(work_type=str(normalized_work_type or "default"), tried=tried)))
    raise RoutedCallError(work_type=str(normalized_work_type or "default"), tried=tried)
