from __future__ import annotations

import asyncio
import heapq
import json
import logging
import os
import pwd
import re
import shutil
import subprocess
import tempfile
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from modes.sdk.runtime.json_normalizer import loads_safe
from session import session_active_cli_name


logger = logging.getLogger(__name__)

_RGLOB_ROLLOUT_LIMIT = 10_000


@dataclass(frozen=True)
class CliProjectRef:
    cli_name: str
    workdir: str
    label: str


@dataclass(frozen=True)
class CliLimitsSnapshot:
    cli_name: str
    status: str
    lines: tuple[str, ...]
    subtitle: str = ""


class CliLimitsService:
    """Собирает доступные лимиты и usage по активным CLI-сессиям."""

    SUPPORTED_CLI_NAMES = ("claude", "codex", "gemini", "grok", "qwen")
    _CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
    _CLAUDE_OAUTH_BETA = "oauth-2025-04-20"
    _CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
    _CODEX_USER_AGENT = "codex_cli_rs/0.111.0 (Linux; x86_64)"
    _GEMINI_USAGE_URL = "https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuota"
    _GEMINI_USER_AGENT = "GeminiCLI/0.31.0/load-code-assist (linux; x86_64)"
    _GEMINI_API_CLIENT_HEADER = "google-genai-sdk/1.41.0 gl-node/v22.19.0"
    _GEMINI_OAUTH_CLIENT_ID = "681255809395-oo8ft2oprdrnp9e3aqf6av3hmdib135j.apps.googleusercontent.com"
    _GROK_USAGE_WINDOW_RE = re.compile(
        r"^\s*(Weekly|Monthly)\s+limit:\s*(\d+(?:\.\d+)?)%\s*$",
        re.IGNORECASE | re.MULTILINE,
    )
    _GROK_USAGE_RESET_RE = re.compile(r"^\s*Next\s+reset:\s*([^\r\n]+?)\s*$", re.IGNORECASE | re.MULTILINE)

    def __init__(
        self,
        *,
        codex_sessions_roots: Optional[Sequence[str | Path]] = None,
        claude_projects_roots: Optional[Sequence[str | Path]] = None,
        grok_sessions_roots: Optional[Sequence[str | Path]] = None,
        claude_username: str = "claude-bot",
        network_timeout_sec: float = 5.0,
        gemini_oauth_client_secret: Optional[str] = None,
    ) -> None:
        self._codex_sessions_roots = [Path(item) for item in (codex_sessions_roots or self._default_codex_roots())]
        self._claude_projects_roots = [Path(item) for item in (claude_projects_roots or self._default_claude_roots(claude_username))]
        self._grok_sessions_roots = [Path(item) for item in (grok_sessions_roots or self._default_grok_roots())]
        self._claude_home = self._home_for_user(claude_username) or Path.home()
        self._gemini_oauth_client_secret = str(gemini_oauth_client_secret or "").strip()
        try:
            timeout_value = float(network_timeout_sec)
        except Exception:
            timeout_value = 5.0
        self._network_timeout_sec = max(0.1, timeout_value)

    def set_gemini_oauth_client_secret(self, value: Optional[str]) -> None:
        self._gemini_oauth_client_secret = str(value or "").strip()

    async def describe_for_sessions(
        self,
        sessions: Iterable[Any],
        *,
        available_clis: Optional[Sequence[str]] = None,
        preferred_workdir: Optional[str] = None,
    ) -> str:
        refs_by_cli = self._collect_active_refs(sessions)
        snapshots = await self.collect_for_sessions(
            sessions,
            available_clis=available_clis,
            preferred_workdir=preferred_workdir,
        )
        return self.format_snapshots(
            snapshots,
            active_clis=sorted(refs_by_cli.keys()),
            available_clis=available_clis,
        )

    async def collect_for_sessions(
        self,
        sessions: Iterable[Any],
        *,
        available_clis: Optional[Sequence[str]] = None,
        preferred_workdir: Optional[str] = None,
    ) -> list[CliLimitsSnapshot]:
        active_refs_by_cli = self._collect_active_refs(sessions)
        project_refs = self._collect_project_refs(sessions, preferred_workdir=preferred_workdir)
        target_clis = self._resolve_target_clis(active_refs_by_cli.keys(), available_clis)
        if not target_clis:
            return []
        tasks = [
            asyncio.create_task(
                self._collect_cli_snapshot(
                    cli_name,
                    self._select_refs_for_cli(active_refs_by_cli.get(cli_name), project_refs),
                )
            )
            for cli_name in target_clis
        ]
        return list(await asyncio.gather(*tasks))

    _SEPARATOR = "━━━━━━━━━━━━━━━━━━━━━━━"

    def format_snapshots(
        self,
        snapshots: Sequence[CliLimitsSnapshot],
        *,
        active_clis: Optional[Sequence[str]] = None,
        available_clis: Optional[Sequence[str]] = None,
    ) -> str:
        ordered_active = set(self._normalize_cli_names(active_clis))
        ordered_available = self._normalize_cli_names(available_clis)
        all_names = ordered_available or sorted(ordered_active)
        active_names = [n for n in all_names if n in ordered_active]
        inactive_names = [n for n in all_names if n not in ordered_active]
        header_parts: list[str] = []
        if active_names:
            header_parts.append("🟢 " + " · ".join(active_names))
        if inactive_names:
            header_parts.append("⚫️ " + " · ".join(inactive_names))
        lines: list[str] = ["    ".join(header_parts) if header_parts else "Нет доступных CLI"]
        if not snapshots:
            return "\n".join(lines).strip()
        for snapshot in snapshots:
            lines.append("")
            lines.append(self._SEPARATOR)
            lines.append("")
            cli_icon = self._CLI_ICONS.get(snapshot.cli_name, "")
            header = f"{cli_icon} {snapshot.cli_name}"
            if snapshot.subtitle:
                header += f" — {snapshot.subtitle}"
            lines.append(header)
            lines.append("")
            for item in snapshot.lines:
                lines.append(item)
        return "\n".join(lines).strip()

    async def _collect_cli_snapshot(self, cli_name: str, refs: Sequence[CliProjectRef]) -> CliLimitsSnapshot:
        if cli_name == "codex":
            return await asyncio.to_thread(self._collect_codex_snapshot, refs)
        if cli_name == "claude":
            return await asyncio.to_thread(self._collect_claude_snapshot, refs)
        if cli_name == "gemini":
            return await asyncio.to_thread(self._collect_gemini_snapshot, refs)
        if cli_name == "grok":
            return await asyncio.to_thread(self._collect_grok_snapshot, refs)
        if cli_name == "qwen":
            return CliLimitsSnapshot(
                cli_name="qwen",
                status="unavailable",
                lines=(
                    "⚠️ квоты недоступны (non-interactive)",
                ),
            )
        return CliLimitsSnapshot(
            cli_name=str(cli_name or "cli"),
            status="unsupported",
            lines=("⚠️ квоты не реализованы",),
        )

    @staticmethod
    def _collect_active_refs(sessions: Iterable[Any]) -> dict[str, list[CliProjectRef]]:
        refs_by_cli: dict[str, list[CliProjectRef]] = {}
        seen: set[tuple[str, str]] = set()
        for session in sessions:
            cli_name = str(session_active_cli_name(session) or "").strip().lower()
            if not cli_name:
                continue
            workdir = os.path.realpath(str(getattr(session, "workdir", "") or "").strip())
            if not workdir:
                continue
            key = (cli_name, workdir)
            if key in seen:
                continue
            seen.add(key)
            label = os.path.basename(workdir.rstrip(os.sep)) or workdir
            refs_by_cli.setdefault(cli_name, []).append(
                CliProjectRef(
                    cli_name=cli_name,
                    workdir=workdir,
                    label=label,
                )
            )
        return refs_by_cli

    def _collect_project_refs(
        self,
        sessions: Iterable[Any],
        *,
        preferred_workdir: Optional[str] = None,
    ) -> list[CliProjectRef]:
        candidates: list[tuple[float, CliProjectRef]] = []
        preferred = os.path.realpath(str(preferred_workdir or "").strip()) if preferred_workdir else ""
        seen: set[str] = set()
        for session in sessions:
            workdir = os.path.realpath(str(getattr(session, "workdir", "") or "").strip())
            if not workdir or workdir in seen:
                continue
            seen.add(workdir)
            label = os.path.basename(workdir.rstrip(os.sep)) or workdir
            ts = self._session_sort_ts(session)
            candidates.append((ts, CliProjectRef(cli_name="", workdir=workdir, label=label)))
        if preferred:
            for _ts, ref in candidates:
                if os.path.realpath(ref.workdir) == preferred:
                    return [ref]
        if not candidates:
            return []
        candidates.sort(key=lambda item: item[0], reverse=True)
        return [candidates[0][1]]

    @staticmethod
    def _select_refs_for_cli(
        active_refs: Optional[Sequence[CliProjectRef]],
        fallback_refs: Sequence[CliProjectRef],
    ) -> list[CliProjectRef]:
        if active_refs:
            if fallback_refs:
                target_workdir = os.path.realpath(fallback_refs[0].workdir)
                for ref in active_refs:
                    if os.path.realpath(ref.workdir) == target_workdir:
                        return [ref]
            return [active_refs[0]]
        return list(fallback_refs)

    @staticmethod
    def _session_sort_ts(session: Any) -> float:
        for attr in ("last_output_ts", "last_tick_ts", "started_at"):
            value = getattr(session, attr, None)
            try:
                if value is not None:
                    return float(value)
            except Exception:
                continue
        return 0.0

    def _resolve_target_clis(
        self,
        active_clis: Iterable[str],
        available_clis: Optional[Sequence[str]],
    ) -> list[str]:
        ordered = self._normalize_cli_names(available_clis)
        for cli_name in self._normalize_cli_names(tuple(active_clis)):
            if cli_name not in ordered:
                ordered.append(cli_name)
        return ordered

    def _collect_codex_snapshot(self, refs: Sequence[CliProjectRef]) -> CliLimitsSnapshot:
        direct_usage = self._read_codex_direct_usage()
        target_workdirs = {os.path.realpath(ref.workdir) for ref in refs}
        file_candidates = self._iter_matching_codex_files(target_workdirs)
        if not file_candidates:
            direct_lines = self._format_codex_direct_usage_lines(direct_usage)
            if direct_lines:
                return CliLimitsSnapshot(cli_name="codex", status="partial", lines=tuple(direct_lines))
            return CliLimitsSnapshot(
                cli_name="codex",
                status="no_data",
                lines=("⚠️ session file не найден для текущих проектов",),
            )
        jsonl_path, matched_workdir = file_candidates[0]
        token_payload = self._read_last_codex_token_payload(jsonl_path)
        if token_payload is None:
            return CliLimitsSnapshot(
                cli_name="codex",
                status="no_data",
                lines=(f"⚠️ session file найден, но token_count в {matched_workdir} отсутствует",),
            )
        project_label = os.path.basename(matched_workdir.rstrip(os.sep)) or matched_workdir
        codex_config = self._read_codex_config()
        subtitle_parts = [project_label]
        if codex_config.get("model"):
            model_part = str(codex_config["model"])
            effort = str(codex_config.get("reasoning") or "").strip()
            if effort:
                model_part += f" ({effort})"
            subtitle_parts.append(model_part)
        subtitle = " · ".join(subtitle_parts)
        lines: list[str] = []
        rate_limits = token_payload.get("rate_limits") if isinstance(token_payload, dict) else None
        direct_lines = self._format_codex_direct_usage_lines(direct_usage)
        if direct_lines:
            lines.extend(direct_lines)
        elif isinstance(rate_limits, dict):
            lines.extend(self._format_codex_local_quota_lines(rate_limits))
        info = token_payload.get("info") if isinstance(token_payload, dict) else None
        total_usage = info.get("total_token_usage") if isinstance(info, dict) else None
        tokens_line = self._format_codex_tokens_line(total_usage)
        if tokens_line:
            lines.append(tokens_line)
        return CliLimitsSnapshot(cli_name="codex", status="ok", lines=tuple(lines), subtitle=subtitle)

    def _collect_claude_snapshot(self, refs: Sequence[CliProjectRef]) -> CliLimitsSnapshot:
        usage = self._read_claude_direct_usage()
        quota_lines = self._format_claude_direct_quota_lines(usage)
        model = self._read_claude_active_model()
        subtitle = model or ""
        if quota_lines:
            return CliLimitsSnapshot(
                cli_name="claude",
                status="ok",
                lines=tuple(quota_lines),
                subtitle=subtitle,
            )
        return CliLimitsSnapshot(
            cli_name="claude",
            status="unavailable",
            lines=("⚠️ квоты недоступны",),
            subtitle=subtitle,
        )

    def _read_claude_active_model(self) -> Optional[str]:
        candidates: list[tuple[float, Path]] = []
        projects_root = self._claude_home / ".claude" / "projects"
        if projects_root.is_dir():
            try:
                for project_dir in projects_root.iterdir():
                    if not project_dir.is_dir():
                        continue
                    for jsonl_path in project_dir.glob("*.jsonl"):
                        try:
                            candidates.append((jsonl_path.stat().st_mtime, jsonl_path))
                        except Exception:
                            pass
            except Exception:
                pass
        if not candidates:
            return self._read_claude_model_from_stats()
        candidates.sort(key=lambda item: item[0], reverse=True)
        for _mtime, jsonl_path in candidates[:3]:
            model = self._extract_claude_model_from_jsonl(jsonl_path)
            if model:
                return model
        return self._read_claude_model_from_stats()

    def _read_claude_model_from_stats(self) -> Optional[str]:
        stats_path = self._claude_home / ".claude" / "stats-cache.json"
        payload = self._read_json_file(stats_path)
        if not isinstance(payload, dict):
            return None
        daily = payload.get("dailyModelTokens")
        if not isinstance(daily, list):
            return None
        for entry in reversed(daily):
            tokens_by_model = entry.get("tokensByModel")
            if not isinstance(tokens_by_model, dict):
                continue
            for model_name in tokens_by_model:
                name = str(model_name).strip()
                if name and not name.startswith("<"):
                    return name
        return None

    @staticmethod
    def _extract_claude_model_from_jsonl(jsonl_path: Path) -> Optional[str]:
        try:
            with open(jsonl_path, "r", encoding="utf-8") as handle:
                for raw_line in handle:
                    line = raw_line.strip()
                    if not line:
                        continue
                    record = loads_safe(line)
                    message = record.get("message")
                    if not isinstance(message, dict):
                        continue
                    model = str(message.get("model") or "").strip()
                    if model and not model.startswith("<"):
                        return model
        except Exception:
            pass
        return None

    def _collect_gemini_snapshot(self, refs: Sequence[CliProjectRef]) -> CliLimitsSnapshot:
        model = self._read_gemini_active_model()
        subtitle = model or ""
        lines: list[str] = []
        seen_projects: set[str] = set()
        for ref in refs:
            usage = self._read_gemini_usage_for_workdir(ref.workdir)
            if usage is None:
                continue
            project_id = str(usage.get("project_id") or "").strip()
            dedupe_key = project_id or os.path.realpath(ref.workdir)
            if dedupe_key in seen_projects:
                continue
            seen_projects.add(dedupe_key)
            lines.extend(self._format_gemini_usage_lines(usage))
        if lines:
            return CliLimitsSnapshot(cli_name="gemini", status="ok", lines=tuple(lines), subtitle=subtitle)
        return CliLimitsSnapshot(
            cli_name="gemini",
            status="unavailable",
            lines=(
                "quota query: недоступно для текущих проектов",
            ),
            subtitle=subtitle,
        )

    def _collect_grok_snapshot(self, refs: Sequence[CliProjectRef]) -> CliLimitsSnapshot:
        direct_usage = self._read_grok_direct_usage()
        quota_line = self._format_grok_direct_quota_line(direct_usage)
        target_workdirs = {os.path.realpath(ref.workdir) for ref in refs}
        session_candidates = self._iter_matching_grok_session_dirs(target_workdirs)
        if not session_candidates:
            if quota_line:
                return CliLimitsSnapshot(
                    cli_name="grok",
                    status="ok",
                    lines=(quota_line,),
                )
            return CliLimitsSnapshot(
                cli_name="grok",
                status="no_data",
                lines=("⚠️ session file не найден для текущих проектов",),
            )
        session_dir, matched_workdir = session_candidates[0]
        summary = self._read_json_file(session_dir / "summary.json") or {}
        signals = self._read_json_file(session_dir / "signals.json") or {}
        model = str(
            summary.get("current_model_id")
            or signals.get("primaryModelId")
            or ""
        ).strip()
        project_label = os.path.basename(matched_workdir.rstrip(os.sep)) or matched_workdir
        subtitle = " · ".join(part for part in (project_label, model) if part)
        local_usage_lines = self._format_grok_usage_lines(summary, signals)
        if not local_usage_lines:
            local_usage_lines = ["⚠️ session найден, но usage в signals.json отсутствует"]
        lines = ([quota_line] if quota_line else []) + local_usage_lines
        if not quota_line:
            lines.append("quota: недоступно через Grok CLI; см. https://console.x.ai/team/default/usage")
        return CliLimitsSnapshot(
            cli_name="grok",
            status="ok" if quota_line else "partial",
            lines=tuple(lines),
            subtitle=subtitle,
        )

    def _read_grok_direct_usage(self) -> Optional[dict[str, Any]]:
        tmux_path = shutil.which("tmux")
        grok_path = shutil.which("grok")
        if not tmux_path or not grok_path:
            return None
        deadline = time.monotonic() + self._network_timeout_sec

        with tempfile.TemporaryDirectory(prefix="cli-proxy-grok-usage-") as temp_dir:
            tmux_socket = str(Path(temp_dir) / "tmux.sock")
            grok_socket = str(Path(temp_dir) / "grok.sock")
            probe_workdir = Path(temp_dir) / "workdir"
            probe_workdir.mkdir()
            tmux_prefix = [tmux_path, "-S", tmux_socket]
            probe_env = os.environ.copy()
            real_grok_home = Path(probe_env.get("GROK_HOME") or (Path.home() / ".grok"))
            auth_path = str(probe_env.get("GROK_AUTH_PATH") or (real_grok_home / "auth.json"))
            probe_env["GROK_HOME"] = str(Path(temp_dir) / "grok-home")
            if Path(auth_path).is_file():
                probe_env["GROK_AUTH_PATH"] = auth_path

            def run_tmux(*args: str) -> subprocess.CompletedProcess[str]:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Grok usage query timed out")
                return subprocess.run(
                    [*tmux_prefix, *args],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=max(0.1, remaining),
                    env=probe_env,
                )

            try:
                run_tmux(
                    "new-session",
                    "-d",
                    "-x",
                    "200",
                    "-y",
                    "50",
                    "-s",
                    "grok-usage",
                    "-c",
                    str(probe_workdir),
                    grok_path,
                    "--leader-socket",
                    grok_socket,
                    "--no-auto-update",
                    "--no-alt-screen",
                    "--minimal",
                    "--no-memory",
                    "--no-subagents",
                )
                while time.monotonic() < deadline:
                    pane_text = run_tmux("capture-pane", "-p", "-t", "grok-usage:0.0").stdout
                    if "❯" in pane_text:
                        break
                    time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
                else:
                    raise TimeoutError("Grok interactive prompt did not become ready")

                run_tmux("send-keys", "-l", "-t", "grok-usage:0.0", "--", "/usage show")
                run_tmux("send-keys", "-t", "grok-usage:0.0", "Enter")
                while time.monotonic() < deadline:
                    pane_text = run_tmux("capture-pane", "-p", "-t", "grok-usage:0.0").stdout
                    usage = self._parse_grok_direct_usage_output(pane_text)
                    if usage is not None:
                        return usage
                    time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
                raise TimeoutError("Grok usage output did not become ready")
            except Exception:
                logger.warning("failed to query Grok usage through isolated TUI", exc_info=True)
                return None
            finally:
                try:
                    subprocess.run(
                        [*tmux_prefix, "kill-server"],
                        check=False,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=1.0,
                    )
                except Exception:
                    logger.warning("failed to stop isolated Grok usage tmux server", exc_info=True)

    @classmethod
    def _parse_grok_direct_usage_output(cls, text: str) -> Optional[dict[str, Any]]:
        window_match = cls._GROK_USAGE_WINDOW_RE.search(str(text or ""))
        if window_match is None:
            return None
        reset_match = cls._GROK_USAGE_RESET_RE.search(str(text or ""))
        return {
            "window": window_match.group(1).lower(),
            "used_percent": max(0.0, min(100.0, float(window_match.group(2)))),
            "resets_at": reset_match.group(1).strip() if reset_match is not None else "",
        }

    @staticmethod
    def _format_grok_direct_quota_line(usage: Optional[dict[str, Any]]) -> Optional[str]:
        if not isinstance(usage, dict) or usage.get("used_percent") is None:
            return None
        used_percent = CliLimitsService._safe_float(usage.get("used_percent"), default=-1.0)
        if used_percent < 0:
            return None
        remaining_percent = max(0.0, min(100.0, 100.0 - used_percent))
        indicator = CliLimitsService._status_indicator(remaining_percent)
        bar = CliLimitsService._progress_bar(remaining_percent)
        pct = CliLimitsService._format_percent(remaining_percent)
        window = str(usage.get("window") or "weekly").strip().lower()
        label = "месяц" if window == "monthly" else "неделя"
        reset_text = str(usage.get("resets_at") or "").strip()
        reset_suffix = f" ↻{reset_text}" if reset_text else ""
        return f"{indicator} {label} {bar} {pct} осталось{reset_suffix}"

    @staticmethod
    def _read_gemini_active_model() -> Optional[str]:
        settings_path = Path.home() / ".gemini" / "settings.json"
        try:
            with open(settings_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        model_section = data.get("model")
        if isinstance(model_section, dict):
            name = str(model_section.get("name") or "").strip()
            return name or None
        if isinstance(model_section, str):
            name = model_section.strip()
            return name or None
        return None

    @staticmethod
    def _read_codex_config() -> dict[str, str]:
        config_path = Path.home() / ".codex" / "config.toml"
        result: dict[str, str] = {}
        try:
            with open(config_path, "rb") as f:
                data = tomllib.load(f)
        except Exception:
            return result
        model = str(data.get("model") or "").strip()
        if model:
            result["model"] = model
        reasoning = str(data.get("model_reasoning_effort") or "").strip()
        if reasoning:
            result["reasoning"] = reasoning
        return result

    def _iter_matching_codex_files(self, target_workdirs: set[str]) -> list[tuple[Path, str]]:
        matched: list[tuple[float, Path, str]] = []
        for root in self._codex_sessions_roots:
            if not root.is_dir():
                continue
            try:
                # Берём _RGLOB_ROLLOUT_LIMIT самых свежих по mtime. nlargest проходит
                # весь генератор (как и прежний sorted), но держит в памяти только N,
                # и, в отличие от islice-перед-сортировкой, не теряет новейшие файлы.
                jsonl_files = heapq.nlargest(
                    _RGLOB_ROLLOUT_LIMIT,
                    root.rglob("rollout-*.jsonl"),
                    key=lambda path: path.stat().st_mtime,
                )
            except Exception:
                logger.exception("failed to scan codex sessions root=%s", root)
                continue
            for jsonl_path in jsonl_files:
                cwd = self._read_codex_session_cwd(jsonl_path)
                if cwd and os.path.realpath(cwd) in target_workdirs:
                    try:
                        matched.append((float(jsonl_path.stat().st_mtime), jsonl_path, cwd))
                    except Exception:
                        matched.append((0.0, jsonl_path, cwd))
        matched.sort(key=lambda item: item[0], reverse=True)
        return [(path, cwd) for _mtime, path, cwd in matched]

    def _iter_matching_grok_session_dirs(self, target_workdirs: set[str]) -> list[tuple[Path, str]]:
        matched: list[tuple[float, Path, str]] = []
        if not target_workdirs:
            return []
        encoded_by_workdir = {
            os.path.realpath(workdir): urllib.parse.quote(os.path.realpath(workdir).rstrip(os.sep), safe="")
            for workdir in target_workdirs
            if workdir
        }
        for root in self._grok_sessions_roots:
            if not root.is_dir():
                continue
            for workdir, encoded in encoded_by_workdir.items():
                project_dir = root / encoded
                if not project_dir.is_dir():
                    continue
                try:
                    children = [item for item in project_dir.iterdir() if item.is_dir()]
                except Exception:
                    logger.exception("failed to iterate grok sessions dir=%s", project_dir)
                    continue
                for session_dir in children:
                    marker = session_dir / "summary.json"
                    if not marker.is_file():
                        continue
                    try:
                        mtime = max(
                            marker.stat().st_mtime,
                            (session_dir / "signals.json").stat().st_mtime
                            if (session_dir / "signals.json").is_file()
                            else 0.0,
                        )
                    except Exception:
                        mtime = 0.0
                    matched.append((mtime, session_dir, workdir))
        matched.sort(key=lambda item: item[0], reverse=True)
        return [(path, workdir) for _mtime, path, workdir in matched]

    @staticmethod
    def _read_codex_session_cwd(jsonl_path: Path) -> str:
        try:
            with open(jsonl_path, "r", encoding="utf-8") as handle:
                for _ in range(12):
                    line = handle.readline()
                    if not line:
                        break
                    record = loads_safe(line)
                    if str(record.get("type") or "") != "session_meta":
                        continue
                    payload = record.get("payload")
                    if not isinstance(payload, dict):
                        continue
                    cwd = str(payload.get("cwd") or "").strip()
                    if cwd:
                        return cwd
        except Exception:
            logger.exception("failed to read codex session meta path=%s", jsonl_path)
        return ""

    @staticmethod
    def _read_last_codex_token_payload(jsonl_path: Path) -> Optional[dict[str, Any]]:
        last_payload: Optional[dict[str, Any]] = None
        try:
            with open(jsonl_path, "r", encoding="utf-8") as handle:
                for raw_line in handle:
                    line = raw_line.strip()
                    if not line:
                        continue
                    record = loads_safe(line)
                    if str(record.get("type") or "") != "event_msg":
                        continue
                    payload = record.get("payload")
                    if not isinstance(payload, dict):
                        continue
                    if str(payload.get("type") or "") != "token_count":
                        continue
                    last_payload = payload
        except Exception:
            logger.exception("failed to read codex token payload path=%s", jsonl_path)
            return None
        return last_payload

    def _read_claude_project_usage(self, workdir: str) -> Optional[dict[str, Any]]:
        project_dir = self._find_claude_project_dir(workdir)
        if project_dir is None:
            return None
        try:
            jsonl_files = sorted(project_dir.glob("*.jsonl"), key=lambda item: item.stat().st_mtime, reverse=True)
        except Exception:
            logger.exception("failed to list claude transcripts dir=%s", project_dir)
            return None
        for jsonl_path in jsonl_files:
            usage = self._summarize_claude_usage_file(jsonl_path)
            if usage is not None:
                return usage
        return None

    def _find_claude_project_dir(self, workdir: str) -> Optional[Path]:
        keys = self._claude_project_key_candidates(workdir)
        for root in self._claude_projects_roots:
            if not root.is_dir():
                continue
            for key in keys:
                candidate = root / key
                if candidate.is_dir():
                    return candidate
        target_workdir = os.path.realpath(str(workdir or ""))
        if not target_workdir:
            return None
        for root in self._claude_projects_roots:
            if not root.is_dir():
                continue
            try:
                children = sorted(root.iterdir(), key=lambda item: item.stat().st_mtime, reverse=True)
            except Exception:
                logger.exception("failed to iterate claude project roots root=%s", root)
                continue
            for child in children:
                if not child.is_dir():
                    continue
                try:
                    newest = max(child.glob("*.jsonl"), key=lambda item: item.stat().st_mtime)
                except ValueError:
                    continue
                except Exception:
                    logger.exception("failed to scan claude project dir=%s", child)
                    continue
                try:
                    preview = newest.read_text(encoding="utf-8", errors="ignore")[:4096]
                except Exception:
                    logger.exception("failed to preview claude transcript path=%s", newest)
                    continue
                if target_workdir in preview:
                    return child
        return None

    @staticmethod
    def _claude_project_key_candidates(workdir: str) -> list[str]:
        raw = os.path.realpath(str(workdir or "")).rstrip(os.sep) or str(workdir or "")
        if not raw:
            return []
        compact_key = re.sub(r"[^A-Za-z0-9]+", "-", raw)
        if raw.startswith(os.sep) and not compact_key.startswith("-"):
            compact_key = "-" + compact_key
        compact_key = compact_key.rstrip("-")
        return [compact_key] if compact_key else []

    @staticmethod
    def _summarize_claude_usage_file(jsonl_path: Path) -> Optional[dict[str, Any]]:
        summary = {
            "input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "output_tokens": 0,
            "updated_at": "",
            "model": "",
        }
        saw_usage = False
        try:
            with open(jsonl_path, "r", encoding="utf-8") as handle:
                for raw_line in handle:
                    line = raw_line.strip()
                    if not line:
                        continue
                    record = loads_safe(line)
                    if bool(record.get("isSidechain")):
                        continue
                    if str(record.get("type") or "") == "progress":
                        continue
                    message = record.get("message")
                    if not isinstance(message, dict):
                        continue
                    usage = message.get("usage")
                    if not isinstance(usage, dict):
                        continue
                    input_tokens = CliLimitsService._safe_int(usage.get("input_tokens"))
                    cache_creation = CliLimitsService._safe_int(usage.get("cache_creation_input_tokens"))
                    cache_read = CliLimitsService._safe_int(usage.get("cache_read_input_tokens"))
                    output_tokens = CliLimitsService._safe_int(usage.get("output_tokens"))
                    if input_tokens or cache_creation or cache_read or output_tokens:
                        saw_usage = True
                    summary["input_tokens"] += input_tokens
                    summary["cache_creation_input_tokens"] += cache_creation
                    summary["cache_read_input_tokens"] += cache_read
                    summary["output_tokens"] += output_tokens
                    timestamp = str(record.get("timestamp") or "").strip()
                    if timestamp:
                        summary["updated_at"] = timestamp
                    model = str(message.get("model") or "").strip()
                    if model and not model.startswith("<"):
                        summary["model"] = model
        except Exception:
            logger.exception("failed to summarize claude usage file path=%s", jsonl_path)
            return None
        if not saw_usage:
            return None
        return summary

    @staticmethod
    def _format_rate_limit_line(label: str, payload: Any) -> Optional[str]:
        if not isinstance(payload, dict):
            return None
        used_percent = payload.get("used_percent")
        window_minutes = CliLimitsService._safe_int(payload.get("window_minutes"))
        resets_at = CliLimitsService._safe_int(payload.get("resets_at"))
        parts: list[str] = []
        if used_percent is not None:
            try:
                remaining = max(0.0, 100.0 - float(used_percent))
            except Exception:
                remaining = None
            if remaining is not None:
                parts.append(f"{CliLimitsService._format_percent(remaining)} осталось")
        if window_minutes > 0:
            parts.append(f"окно {window_minutes} мин")
        if resets_at > 0:
            parts.append(f"сброс {CliLimitsService._format_epoch(resets_at)}")
        if not parts:
            return None
        return f"{label}: {', '.join(parts)}"

    @staticmethod
    def _format_codex_credits_line(payload: Any) -> Optional[str]:
        if not isinstance(payload, dict):
            return None
        if bool(payload.get("unlimited")):
            return "credits: unlimited"
        balance = payload.get("balance")
        has_credits = payload.get("has_credits")
        if balance is None and has_credits is None:
            return None
        parts = []
        if has_credits is not None:
            parts.append(f"has_credits={bool(has_credits)}")
        if balance is not None:
            parts.append(f"balance={balance}")
        return f"credits: {', '.join(parts)}" if parts else None

    @staticmethod
    def _format_codex_tokens_line(payload: Any) -> Optional[str]:
        if not isinstance(payload, dict):
            return None
        input_tokens = CliLimitsService._safe_int(payload.get("input_tokens"))
        cached_input = CliLimitsService._safe_int(payload.get("cached_input_tokens"))
        output_tokens = CliLimitsService._safe_int(payload.get("output_tokens"))
        total_tokens = CliLimitsService._safe_int(payload.get("total_tokens"))
        if not total_tokens and not input_tokens:
            return None
        fmt = CliLimitsService._format_compact_number
        detail_parts: list[str] = []
        if input_tokens:
            detail_parts.append(f"↘️ in {fmt(input_tokens)}")
        if cached_input:
            detail_parts.append(f"💾 {fmt(cached_input)}")
        if output_tokens:
            detail_parts.append(f"↗️ out {fmt(output_tokens)}")
        total_text = f"📊 {fmt(total_tokens)} total" if total_tokens else ""
        detail_text = " · ".join(detail_parts)
        if total_text and detail_text:
            return f"{total_text}\n({detail_text})"
        return total_text or detail_text or None

    @staticmethod
    def _format_claude_usage_line(label: str, usage: dict[str, Any]) -> Optional[str]:
        parts = []
        input_tokens = CliLimitsService._safe_int(usage.get("input_tokens"))
        cache_creation = CliLimitsService._safe_int(usage.get("cache_creation_input_tokens"))
        cache_read = CliLimitsService._safe_int(usage.get("cache_read_input_tokens"))
        output_tokens = CliLimitsService._safe_int(usage.get("output_tokens"))
        if input_tokens:
            parts.append(f"in {CliLimitsService._format_int(input_tokens)}")
        if cache_creation:
            parts.append(f"cache write {CliLimitsService._format_int(cache_creation)}")
        if cache_read:
            parts.append(f"cache read {CliLimitsService._format_int(cache_read)}")
        if output_tokens:
            parts.append(f"out {CliLimitsService._format_int(output_tokens)}")
        if not parts:
            return None
        return f"{label}: {', '.join(parts)}"

    def _read_claude_direct_usage(self) -> Optional[dict[str, Any]]:
        credentials = self._read_json_file(self._claude_home / ".claude" / ".credentials.json")
        if not isinstance(credentials, dict):
            return None
        oauth = credentials.get("claudeAiOauth")
        if not isinstance(oauth, dict):
            return None
        access_token = str(oauth.get("accessToken") or "").strip()
        if not access_token:
            return None
        request = urllib.request.Request(
            self._CLAUDE_USAGE_URL,
            headers={
                "Authorization": f"Bearer {access_token}",
                "anthropic-beta": self._CLAUDE_OAUTH_BETA,
                "Accept": "application/json",
            },
            method="GET",
        )
        response_payload = self._request_json(request)
        if not isinstance(response_payload, dict):
            return None
        return response_payload

    def _format_claude_direct_quota_lines(self, usage: Optional[dict[str, Any]]) -> list[str]:
        if not isinstance(usage, dict):
            return []
        lines: list[str] = []
        five_hour = self._format_claude_quota_window("5ч", usage.get("five_hour"))
        if five_hour:
            lines.append(five_hour)
        seven_day = self._format_claude_quota_window("7д", usage.get("seven_day"))
        if seven_day:
            lines.append(seven_day)
        return lines

    @staticmethod
    def _format_claude_quota_window(label: str, payload: Any) -> Optional[str]:
        if not isinstance(payload, dict):
            return None
        utilization = payload.get("utilization")
        if utilization is None:
            return None
        try:
            remaining = max(0.0, 100.0 - float(utilization))
        except Exception:
            return None
        indicator = CliLimitsService._status_indicator(remaining)
        bar = CliLimitsService._progress_bar(remaining)
        pct = CliLimitsService._format_percent(remaining)
        reset_text = ""
        reset_raw = str(payload.get("resets_at") or "").strip()
        if reset_raw:
            reset_text = f" ↻{CliLimitsService._format_datetime(reset_raw)}"
        return f"{indicator} {label} {bar} {pct}{reset_text}"

    def _read_codex_direct_usage(self) -> Optional[dict[str, Any]]:
        auth_path = Path.home() / ".codex" / "auth.json"
        payload = self._read_json_file(auth_path)
        if not isinstance(payload, dict):
            return None
        tokens = payload.get("tokens")
        if not isinstance(tokens, dict):
            return None
        access_token = str(tokens.get("access_token") or "").strip()
        account_id = str(tokens.get("account_id") or "").strip()
        if not access_token or not account_id:
            return None
        request = urllib.request.Request(
            self._CODEX_USAGE_URL,
            headers={
                "user-agent": self._CODEX_USER_AGENT,
                "authorization": f"Bearer {access_token}",
                "chatgpt-account-id": account_id,
                "accept": "*/*",
                "host": "chatgpt.com",
                "Connection": "close",
            },
            method="GET",
        )
        response_payload = self._request_json(request)
        if not isinstance(response_payload, dict):
            return None
        rate_limit = response_payload.get("rate_limit")
        if not isinstance(rate_limit, dict):
            return None
        return {
            "email": str(response_payload.get("email") or "").strip(),
            "plan_type": str(response_payload.get("plan_type") or "").strip(),
            "primary_window": rate_limit.get("primary_window"),
            "secondary_window": rate_limit.get("secondary_window"),
            "credits": response_payload.get("credits"),
        }

    def _format_codex_direct_usage_lines(self, usage: Optional[dict[str, Any]]) -> list[str]:
        if not isinstance(usage, dict):
            return []
        return self._format_codex_combined_quota_lines(
            usage.get("primary_window"),
            usage.get("secondary_window"),
            direct_api=True,
        )

    def _format_codex_local_quota_lines(self, rate_limits: Any) -> list[str]:
        if not isinstance(rate_limits, dict):
            return []
        return self._format_codex_combined_quota_lines(
            rate_limits.get("primary"),
            rate_limits.get("secondary"),
            direct_api=False,
        )

    def _format_codex_combined_quota_lines(
        self,
        primary_payload: Any,
        secondary_payload: Any,
        *,
        direct_api: bool,
    ) -> list[str]:
        lines: list[str] = []
        primary_line = self._format_codex_window_summary("primary", primary_payload, direct_api=direct_api)
        if primary_line:
            lines.append(primary_line)
        secondary_line = self._format_codex_window_summary("secondary", secondary_payload, direct_api=direct_api)
        if secondary_line:
            lines.append(secondary_line)
        return lines

    @staticmethod
    def _format_codex_window_summary(label: str, payload: Any, *, direct_api: bool) -> Optional[str]:
        if not isinstance(payload, dict):
            return None
        used_percent = payload.get("used_percent")
        remaining: Optional[float] = None
        if used_percent is not None:
            try:
                remaining = max(0.0, 100.0 - float(used_percent))
            except Exception:
                remaining = None
        if remaining is None:
            return None
        bar = CliLimitsService._progress_bar(remaining)
        pct = CliLimitsService._format_percent(remaining)
        reset_text = ""
        if direct_api:
            reset_at = CliLimitsService._safe_int(payload.get("reset_at"))
            reset_after_seconds = CliLimitsService._safe_int(payload.get("reset_after_seconds"))
            window_seconds = CliLimitsService._safe_int(payload.get("limit_window_seconds"))
            if reset_at > 0:
                reset_text = f" ↻{CliLimitsService._format_epoch(reset_at)}"
            elif reset_after_seconds > 0:
                reset_text = f" ↻через {CliLimitsService._format_duration(reset_after_seconds)}"
            elif window_seconds > 0:
                reset_text = f" окно {CliLimitsService._format_duration(window_seconds)}"
        else:
            resets_at = CliLimitsService._safe_int(payload.get("resets_at"))
            window_minutes = CliLimitsService._safe_int(payload.get("window_minutes"))
            if resets_at > 0:
                reset_text = f" ↻{CliLimitsService._format_epoch(resets_at)}"
            elif window_minutes > 0:
                reset_text = f" окно {window_minutes} мин"
        return f"💎 {label} {bar} {pct}{reset_text}"

    def _read_gemini_usage_for_workdir(self, workdir: str) -> Optional[dict[str, Any]]:
        project_id = self._find_gemini_project_id(workdir)
        if not project_id:
            return None
        credentials = self._read_json_file(Path.home() / ".gemini" / "oauth_creds.json")
        if not isinstance(credentials, dict):
            return None
        if self._gemini_token_expiring(credentials):
            refreshed = self._refresh_gemini_credentials(credentials)
            if refreshed is not None:
                credentials = refreshed
        response_payload = self._request_gemini_quota(project_id, credentials)
        if response_payload is None:
            refreshed = self._refresh_gemini_credentials(credentials)
            if refreshed is None or refreshed == credentials:
                return None
            response_payload = self._request_gemini_quota(project_id, refreshed)
        if not isinstance(response_payload, dict):
            return None
        buckets = response_payload.get("buckets")
        if not isinstance(buckets, list) or not buckets:
            return None
        models_by_id: dict[str, dict[str, Any]] = {}
        for bucket in buckets:
            if not isinstance(bucket, dict):
                continue
            model_id = str(bucket.get("modelId") or "").strip()
            if not model_id:
                continue
            candidate = {
                "model_id": model_id,
                "remaining_fraction": bucket.get("remainingFraction"),
                "reset_time": bucket.get("resetTime"),
            }
            current = models_by_id.get(model_id)
            if current is None or self._prefer_gemini_quota_candidate(candidate, current):
                models_by_id[model_id] = candidate
        if not models_by_id:
            return None
        return {
            "project_id": project_id,
            "models": [models_by_id[key] for key in sorted(models_by_id.keys())],
        }

    @staticmethod
    def _prefer_gemini_quota_candidate(candidate: dict[str, Any], current: dict[str, Any]) -> bool:
        candidate_remaining = CliLimitsService._safe_float(candidate.get("remaining_fraction"), default=1.0)
        current_remaining = CliLimitsService._safe_float(current.get("remaining_fraction"), default=1.0)
        if candidate_remaining != current_remaining:
            return candidate_remaining < current_remaining
        candidate_reset = str(candidate.get("reset_time") or "").strip()
        current_reset = str(current.get("reset_time") or "").strip()
        if candidate_reset and not current_reset:
            return True
        if candidate_reset and current_reset:
            return candidate_reset < current_reset
        return False

    def _request_gemini_quota(self, project_id: str, credentials: dict[str, Any]) -> Optional[dict[str, Any]]:
        access_token = str(credentials.get("access_token") or "").strip()
        if not access_token or not project_id:
            return None
        request = urllib.request.Request(
            self._GEMINI_USAGE_URL,
            data=json.dumps({"project": project_id}).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
                "User-Agent": self._GEMINI_USER_AGENT,
                "X-Goog-Api-Client": self._GEMINI_API_CLIENT_HEADER,
            },
            method="POST",
        )
        return self._request_json(request)

    def _refresh_gemini_credentials(self, credentials: dict[str, Any]) -> Optional[dict[str, Any]]:
        refresh_token = str(credentials.get("refresh_token") or "").strip()
        if not refresh_token:
            return None
        client_secret = self._gemini_oauth_client_secret
        if not client_secret:
            return None
        body = urllib.parse.urlencode(
            {
                "client_id": self._GEMINI_OAUTH_CLIENT_ID,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            "https://oauth2.googleapis.com/token",
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        refreshed = self._request_json(request)
        if not isinstance(refreshed, dict):
            return None
        access_token = str(refreshed.get("access_token") or "").strip()
        if not access_token:
            return None
        merged = dict(credentials)
        merged["access_token"] = access_token
        token_type = str(refreshed.get("token_type") or "").strip()
        if token_type:
            merged["token_type"] = token_type
        expires_in = CliLimitsService._safe_int(refreshed.get("expires_in"))
        if expires_in > 0:
            merged["expiry_date"] = int(time.time() * 1000) + expires_in * 1000
        return merged

    def _find_gemini_project_id(self, workdir: str) -> str:
        payload = self._read_json_file(Path.home() / ".gemini" / "projects.json")
        if not isinstance(payload, dict):
            return ""
        projects = payload.get("projects")
        if not isinstance(projects, dict):
            return ""
        target = os.path.realpath(str(workdir or "")).rstrip(os.sep)
        if not target:
            return ""
        direct = projects.get(target)
        if isinstance(direct, str) and direct.strip():
            return direct.strip()
        best_match = ""
        best_project = ""
        for raw_root, raw_project in projects.items():
            project_id = str(raw_project or "").strip()
            root = os.path.realpath(str(raw_root or "")).rstrip(os.sep)
            if not project_id or not root:
                continue
            if target == root or target.startswith(root + os.sep):
                if len(root) > len(best_match):
                    best_match = root
                    best_project = project_id
        return best_project

    @staticmethod
    def _gemini_token_expiring(credentials: dict[str, Any]) -> bool:
        expiry_date = CliLimitsService._safe_int(credentials.get("expiry_date"))
        if expiry_date <= 0:
            return False
        return expiry_date <= int(time.time() * 1000) + 60_000

    def _format_gemini_usage_lines(self, usage: dict[str, Any]) -> list[str]:
        models = usage.get("models")
        if not isinstance(models, list):
            return []
        lines: list[str] = []
        for item in sorted(models, key=lambda value: str(value.get("model_id") or "")):
            line = self._format_gemini_model_line(item)
            if line:
                lines.append(line)
        return lines

    @staticmethod
    def _format_gemini_model_line(item: Any) -> Optional[str]:
        if not isinstance(item, dict):
            return None
        model_id = str(item.get("model_id") or "").strip()
        if not model_id:
            return None
        remaining_fraction = item.get("remaining_fraction")
        if remaining_fraction is None:
            return None
        try:
            remaining_percent = max(0.0, min(100.0, float(remaining_fraction) * 100.0))
        except Exception:
            return None
        indicator = CliLimitsService._status_indicator(remaining_percent)
        bar = CliLimitsService._progress_bar(remaining_percent)
        pct = CliLimitsService._format_percent(remaining_percent)
        reset_text = ""
        reset_time = str(item.get("reset_time") or "").strip()
        if reset_time:
            reset_text = f" ↻{CliLimitsService._format_datetime(reset_time)}"
        return f"{indicator} {model_id} {bar} {pct}{reset_text}"

    def _format_grok_usage_lines(self, summary: dict[str, Any], signals: dict[str, Any]) -> list[str]:
        lines: list[str] = []
        context_tokens = self._safe_int(signals.get("contextTokensUsed"))
        context_window = self._safe_int(signals.get("contextWindowTokens"))
        remaining_percent: Optional[float] = None
        if context_tokens > 0 and context_window > 0:
            used_percent = min(100.0, max(0.0, context_tokens / context_window * 100.0))
            remaining_percent = max(0.0, 100.0 - used_percent)
        else:
            raw_usage = signals.get("contextWindowUsage")
            if raw_usage is not None:
                try:
                    remaining_percent = max(0.0, 100.0 - float(raw_usage))
                except Exception:
                    remaining_percent = None
        if remaining_percent is not None:
            indicator = self._status_indicator(remaining_percent)
            bar = self._progress_bar(remaining_percent)
            pct = self._format_percent(remaining_percent)
            lines.append(f"{indicator} context {bar} {pct}")
        if context_tokens > 0:
            token_line = f"📊 context {self._format_compact_number(context_tokens)}"
            if context_window > 0:
                token_line += f" / {self._format_compact_number(context_window)}"
            lines.append(token_line)
        turn_count = self._safe_int(signals.get("turnCount"))
        tool_count = self._safe_int(signals.get("toolCallCount"))
        user_count = self._safe_int(signals.get("userMessageCount"))
        assistant_count = self._safe_int(signals.get("assistantMessageCount"))
        detail_parts: list[str] = []
        if turn_count:
            detail_parts.append(f"turns {turn_count}")
        if tool_count:
            detail_parts.append(f"tools {tool_count}")
        if user_count or assistant_count:
            detail_parts.append(f"messages {user_count}/{assistant_count}")
        if detail_parts:
            lines.append(" · ".join(detail_parts))
        updated_at = str(summary.get("updated_at") or summary.get("last_active_at") or "").strip()
        if updated_at:
            lines.append(f"updated: {self._format_datetime(updated_at)}")
        return lines

    @staticmethod
    def _normalize_cli_names(items: Optional[Sequence[str]]) -> list[str]:
        ordered: list[str] = []
        seen: set[str] = set()
        for raw_item in items or ():
            item = str(raw_item or "").strip().lower()
            if not item or item in seen:
                continue
            seen.add(item)
            ordered.append(item)
        return ordered

    def _request_json(self, request: urllib.request.Request) -> Optional[dict[str, Any]]:
        try:
            with urllib.request.urlopen(request, timeout=self._network_timeout_sec) as response:
                payload = loads_safe(response.read().decode("utf-8"))
        except urllib.error.HTTPError:
            return None
        except urllib.error.URLError:
            return None
        except TimeoutError:
            return None
        except Exception:
            logger.exception("limits request failed url=%s", getattr(request, "full_url", ""))
            return None
        if not isinstance(payload, dict):
            return None
        return payload

    @staticmethod
    def _read_json_file(path: Path) -> Optional[dict[str, Any]]:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except FileNotFoundError:
            return None
        except Exception:
            logger.exception("failed to read json file path=%s", path)
            return None
        if not isinstance(payload, dict):
            return None
        return payload

    @staticmethod
    def _default_codex_roots() -> list[Path]:
        return [Path.home() / ".codex" / "sessions"]

    @staticmethod
    def _default_claude_roots(claude_username: str) -> list[Path]:
        roots: list[Path] = []
        seen: set[str] = set()
        for home in (Path.home(), CliLimitsService._home_for_user(claude_username)):
            if home is None:
                continue
            candidate = home / ".claude" / "projects"
            key = str(candidate)
            if key in seen:
                continue
            seen.add(key)
            roots.append(candidate)
        return roots

    @staticmethod
    def _default_grok_roots() -> list[Path]:
        return [Path.home() / ".grok" / "sessions"]

    @staticmethod
    def _home_for_user(username: str) -> Optional[Path]:
        name = str(username or "").strip()
        if not name:
            return None
        try:
            return Path(pwd.getpwnam(name).pw_dir)
        except Exception:
            return None

    @staticmethod
    def _safe_int(value: Any) -> int:
        try:
            return int(value or 0)
        except Exception:
            return 0

    @staticmethod
    def _safe_float(value: Any, *, default: float = 0.0) -> float:
        try:
            return float(value)
        except Exception:
            return float(default)

    _CLI_ICONS: dict[str, str] = {
        "codex": "📦",
        "claude": "🤖",
        "gemini": "♊",
        "grok": "✕",
        "qwen": "🔮",
    }

    @staticmethod
    def _progress_bar(percent: float, width: int = 10) -> str:
        clamped = max(0.0, min(100.0, float(percent)))
        filled = round(clamped / 100.0 * width)
        return "█" * filled + "░" * (width - filled)

    @staticmethod
    def _status_indicator(percent: float) -> str:
        if percent > 80.0:
            return "🟢"
        if percent >= 40.0:
            return "🟡"
        return "🔴"

    @staticmethod
    def _format_compact_number(value: int) -> str:
        if value >= 1_000_000:
            result = value / 1_000_000
            return f"{result:.1f}M" if result < 100 else f"{int(round(result))}M"
        if value >= 1_000:
            result = value / 1_000
            return f"{result:.0f}K" if result >= 10 else f"{result:.1f}K"
        return str(value)

    @staticmethod
    def _format_int(value: int) -> str:
        return f"{int(value):,}".replace(",", " ")

    @staticmethod
    def _format_percent(value: float) -> str:
        rounded = round(float(value), 1)
        if abs(rounded - round(rounded)) < 1e-9:
            return f"{int(round(rounded))}%"
        return f"{rounded:.1f}%"

    @staticmethod
    def _format_epoch(value: int) -> str:
        try:
            return time.strftime("%Y-%m-%d %H:%M", time.localtime(int(value)))
        except Exception:
            return str(value)

    @staticmethod
    def _format_datetime(value: str) -> str:
        raw = str(value or "").strip()
        if not raw:
            return ""
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return parsed.astimezone().strftime("%Y-%m-%d %H:%M")
        except Exception:
            return raw

    @staticmethod
    def _format_duration(value: int) -> str:
        seconds = max(0, int(value))
        days, remainder = divmod(seconds, 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes = remainder // 60
        parts: list[str] = []
        if days > 0:
            parts.append(f"{days}д")
        if hours > 0:
            parts.append(f"{hours}ч")
        if minutes > 0 or not parts:
            parts.append(f"{minutes}м")
        return " ".join(parts)
