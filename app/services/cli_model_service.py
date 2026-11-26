"""Каталог моделей и уровней рассуждений для основных CLI и подстановка флагов запуска."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence

logger = logging.getLogger(__name__)

MODEL_SUPPORTED_CLIS: tuple[str, ...] = ("agy", "claude", "codex", "grok")

_CACHE_TTL_SEC = 300.0
_CLI_DISCOVERY_TIMEOUT_SEC = 8.0

_DEFAULT_EFFORTS_BY_CLI: dict[str, tuple[str, ...]] = {
    "agy": ("low", "medium", "high", "xhigh", "max"),
    "claude": ("low", "medium", "high", "xhigh", "max"),
    "codex": ("low", "medium", "high", "xhigh"),
    "grok": ("low", "medium", "high"),
}

_CLAUDE_FALLBACK_MODELS: tuple[tuple[str, str], ...] = (
    ("opus", "Opus"),
    ("sonnet", "Sonnet"),
    ("haiku", "Haiku"),
    ("fable", "Fable"),
)

_CODEX_FALLBACK_MODELS: tuple[tuple[str, str], ...] = (
    ("gpt-5.4", "gpt-5.4"),
    ("gpt-5.3-codex", "gpt-5.3-codex"),
    ("o3", "o3"),
    ("o4-mini", "o4-mini"),
)


@dataclass(frozen=True)
class CliModelOption:
    id: str
    label: str
    efforts: tuple[str, ...] = ()


@dataclass(frozen=True)
class CliModelCatalog:
    cli_name: str
    supported: bool
    models: tuple[CliModelOption, ...] = ()
    efforts: tuple[str, ...] = ()
    default_model: str = ""
    default_effort: str = ""

    def efforts_for_model(self, model_id: Optional[str]) -> tuple[str, ...]:
        selected = str(model_id or "").strip()
        if selected:
            for item in self.models:
                if item.id == selected and item.efforts:
                    return item.efforts
        return self.efforts

    def label_for_model(self, model_id: Optional[str]) -> str:
        selected = str(model_id or "").strip()
        if not selected:
            return ""
        for item in self.models:
            if item.id == selected:
                return item.label
        return selected


_CATALOG_CACHE: dict[str, tuple[float, CliModelCatalog]] = {}


def is_model_supported_cli(cli_name: Optional[str]) -> bool:
    return str(cli_name or "").strip().lower() in MODEL_SUPPORTED_CLIS


def clear_cli_model_cache(cli_name: Optional[str] = None) -> None:
    if cli_name is None:
        _CATALOG_CACHE.clear()
        return
    _CATALOG_CACHE.pop(str(cli_name or "").strip().lower(), None)


def get_cli_model_catalog(cli_name: Optional[str], *, force_refresh: bool = False) -> CliModelCatalog:
    name = str(cli_name or "").strip().lower()
    if name not in MODEL_SUPPORTED_CLIS:
        return CliModelCatalog(cli_name=name, supported=False)
    now = time.monotonic()
    if not force_refresh:
        cached = _CATALOG_CACHE.get(name)
        if cached is not None and now - cached[0] < _CACHE_TTL_SEC:
            return cached[1]
    catalog = _build_catalog(name)
    _CATALOG_CACHE[name] = (now, catalog)
    return catalog


def _build_catalog(cli_name: str) -> CliModelCatalog:
    if cli_name == "agy":
        return _build_agy_catalog()
    if cli_name == "claude":
        return _build_claude_catalog()
    if cli_name == "codex":
        return _build_codex_catalog()
    if cli_name == "grok":
        return _build_grok_catalog()
    return CliModelCatalog(cli_name=cli_name, supported=False)


def _run_cli_stdout(argv: Sequence[str]) -> str:
    try:
        res = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            timeout=_CLI_DISCOVERY_TIMEOUT_SEC,
            check=False,
        )
    except Exception:
        logger.debug("cli model discovery command failed argv=%s", argv, exc_info=True)
        return ""
    if res.returncode != 0:
        return ""
    return str(res.stdout or "")


def _read_json(path: Path) -> Optional[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _build_agy_catalog() -> CliModelCatalog:
    efforts = _DEFAULT_EFFORTS_BY_CLI["agy"]
    settings = _read_json(Path.home() / ".gemini" / "antigravity-cli" / "settings.json") or {}
    default_model = str(settings.get("model") or "").strip()
    models: list[CliModelOption] = []
    seen: set[str] = set()
    binary = shutil.which("agy")
    if binary:
        stdout = _run_cli_stdout([binary, "models"])
        for raw_line in stdout.splitlines():
            line = raw_line.strip()
            if not line or "Fetching available models" in line:
                continue
            parts = line.split(None, 1)
            if not parts:
                continue
            model_id = parts[0].strip()
            if not model_id or model_id in seen or model_id.startswith(("Usage:", "Flags:", "-")):
                continue
            label = parts[1].strip() if len(parts) > 1 else model_id
            seen.add(model_id)
            models.append(CliModelOption(id=model_id, label=label, efforts=efforts))
    return CliModelCatalog(
        cli_name="agy",
        supported=True,
        models=tuple(models),
        efforts=efforts,
        default_model=default_model,
    )


def _build_claude_catalog() -> CliModelCatalog:
    efforts = _DEFAULT_EFFORTS_BY_CLI["claude"]
    models: list[CliModelOption] = [
        CliModelOption(id=mid, label=label, efforts=efforts) for mid, label in _CLAUDE_FALLBACK_MODELS
    ]
    seen = {item.id for item in models}
    default_model = ""
    stats = _read_json(Path.home() / ".claude" / "stats-cache.json")
    if isinstance(stats, dict):
        daily = stats.get("dailyModelTokens")
        if isinstance(daily, list):
            for entry in reversed(daily):
                tokens_by_model = entry.get("tokensByModel") if isinstance(entry, dict) else None
                if not isinstance(tokens_by_model, dict):
                    continue
                for model_name in tokens_by_model:
                    cleaned = str(model_name or "").strip()
                    if not cleaned or cleaned.startswith("<"):
                        continue
                    if not default_model:
                        default_model = cleaned
                    if cleaned not in seen:
                        seen.add(cleaned)
                        models.append(CliModelOption(id=cleaned, label=cleaned, efforts=efforts))
    return CliModelCatalog(
        cli_name="claude",
        supported=True,
        models=tuple(models),
        efforts=efforts,
        default_model=default_model,
    )


def _build_codex_catalog() -> CliModelCatalog:
    default_model = ""
    default_effort = ""
    config_path = Path.home() / ".codex" / "config.toml"
    try:
        with open(config_path, "rb") as handle:
            cfg = tomllib.load(handle)
        default_model = str(cfg.get("model") or "").strip()
        default_effort = str(cfg.get("model_reasoning_effort") or "").strip()
    except Exception:
        pass

    models: list[CliModelOption] = []
    all_efforts: list[str] = []
    cache = _read_json(Path.home() / ".codex" / "models_cache.json")
    raw_models = cache.get("models") if isinstance(cache, dict) else None
    if isinstance(raw_models, list):
        seen: set[str] = set()
        for item in raw_models:
            if not isinstance(item, dict):
                continue
            if str(item.get("visibility") or "").strip().lower() not in ("", "list"):
                continue
            slug = str(item.get("slug") or "").strip()
            if not slug or slug in seen:
                continue
            seen.add(slug)
            levels = []
            for lvl in item.get("supported_reasoning_levels") or ():
                if isinstance(lvl, dict):
                    eff = str(lvl.get("effort") or "").strip().lower()
                else:
                    eff = str(lvl or "").strip().lower()
                if eff and eff not in levels:
                    levels.append(eff)
                    if eff not in all_efforts:
                        all_efforts.append(eff)
            item_efforts = tuple(levels) or _DEFAULT_EFFORTS_BY_CLI["codex"]
            models.append(CliModelOption(id=slug, label=slug, efforts=item_efforts))

    if not models:
        efforts = _DEFAULT_EFFORTS_BY_CLI["codex"]
        models = [CliModelOption(id=mid, label=label, efforts=efforts) for mid, label in _CODEX_FALLBACK_MODELS]
    efforts_tuple = tuple(all_efforts) or _DEFAULT_EFFORTS_BY_CLI["codex"]
    return CliModelCatalog(
        cli_name="codex",
        supported=True,
        models=tuple(models),
        efforts=efforts_tuple,
        default_model=default_model,
        default_effort=default_effort,
    )


def _build_grok_catalog() -> CliModelCatalog:
    efforts = _DEFAULT_EFFORTS_BY_CLI["grok"]
    models: list[CliModelOption] = []
    default_model = ""
    seen: set[str] = set()
    binary = shutil.which("grok")
    if binary:
        stdout = _run_cli_stdout([binary, "models"])
        for raw_line in stdout.splitlines():
            line = raw_line.strip()
            if line.lower().startswith("default model:"):
                default_model = line.split(":", 1)[1].strip()
                continue
            if line.startswith(("* ", "- ")):
                token = line[2:].strip().split()[0]
                if token and token not in seen:
                    seen.add(token)
                    models.append(CliModelOption(id=token, label=token, efforts=efforts))
    if default_model and default_model not in seen:
        models.insert(0, CliModelOption(id=default_model, label=default_model, efforts=efforts))
    return CliModelCatalog(
        cli_name="grok",
        supported=True,
        models=tuple(models),
        efforts=efforts,
        default_model=default_model,
    )


def _strip_flag_with_value(cmd: list[str], flags: set[str]) -> list[str]:
    result: list[str] = []
    idx = 0
    while idx < len(cmd):
        part = cmd[idx]
        if part in flags:
            idx += 2
            continue
        if any(part.startswith(flag + "=") for flag in flags):
            idx += 1
            continue
        result.append(part)
        idx += 1
    return result


def _strip_codex_config_key(cmd: list[str], key: str) -> list[str]:
    prefix = f"{key}="
    result: list[str] = []
    idx = 0
    while idx < len(cmd):
        part = cmd[idx]
        if part in {"-c", "--config"} and idx + 1 < len(cmd) and str(cmd[idx + 1]).startswith(prefix):
            idx += 2
            continue
        if (part.startswith("-c=") or part.startswith("--config=")) and part.split("=", 1)[1].startswith(prefix):
            idx += 1
            continue
        result.append(part)
        idx += 1
    return result


def apply_cli_model_args(
    cli_name: Optional[str],
    cmd: Sequence[str],
    *,
    model: Optional[str] = None,
    effort: Optional[str] = None,
) -> list[str]:
    """Подставляет флаги модели и уровня рассуждений в команду запуска CLI (headless и tmux)."""
    name = str(cli_name or "").strip().lower()
    args = [str(part) for part in cmd]
    selected_model = str(model or "").strip()
    selected_effort = str(effort or "").strip().lower()
    if name not in MODEL_SUPPORTED_CLIS or not args or (not selected_model and not selected_effort):
        return args

    insert: list[str] = []
    if name in {"agy", "claude"}:
        if selected_model:
            args = _strip_flag_with_value(args, {"--model", "-m"})
            insert.extend(["--model", selected_model])
        if selected_effort:
            args = _strip_flag_with_value(args, {"--effort"})
            insert.extend(["--effort", selected_effort])
    elif name == "grok":
        if selected_model:
            args = _strip_flag_with_value(args, {"--model", "-m"})
            insert.extend(["--model", selected_model])
        if selected_effort:
            args = _strip_flag_with_value(args, {"--reasoning-effort", "--effort"})
            insert.extend(["--reasoning-effort", selected_effort])
    elif name == "codex":
        if selected_model:
            args = _strip_flag_with_value(args, {"--model", "-m"})
            args = _strip_codex_config_key(args, "model")
            insert.extend(["-m", selected_model])
        if selected_effort:
            args = _strip_codex_config_key(args, "model_reasoning_effort")
            insert.extend(["-c", f"model_reasoning_effort={selected_effort}"])

    if not insert:
        return args
    # Вставляем сразу после имени бинарника (индекс 1), чтобы глобальные флаги
    # корректно работали для всех подкоманд (codex exec / codex resume / agy / claude / grok).
    return [args[0], *insert, *args[1:]]
