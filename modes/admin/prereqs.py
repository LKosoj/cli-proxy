"""
Prereqs: список утилит, которые admin-режим ожидает на целевом сервере.

Состоит из трёх слоёв:

1. Manifest (PREREQS_REQUIRED, PREREQS_RECOMMENDED) — фиксированный набор CLI-утилит
   с привязкой к distro-specific package manager'ам (apt, dnf/yum, apk, pacman).
   Required — без них деградирует core-функционал (runbook bash -c, shell validation).
   Recommended — улучшают observability (shellcheck static analysis, jq для health checks).

2. Check (build_prereqs_check) — BaselineCheck, который scanner выполнит на хосте:
   команда `command -v <tool>` по списку, вывод парсится в dict {tool: bool}.
   Результат становится частью baseline под ключом `admin.prereqs`.

3. Evaluation + bootstrap (evaluate_prereqs, generate_bootstrap_script) —
   по результату даёт PrereqsReport и идемпотентный shell-скрипт для ручного запуска
   через bootstrap-runbook (manual-only, confidence=0.0).

Bootstrap-скрипт не ставит пакеты автоматически; он лишь собирается для оператора,
который затем запускает его через runbook_step (dry_run first, потом promote).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple


@dataclass(frozen=True)
class PrereqTool:
    """Одна строка manifest'а: CLI-имя + пакет по пакет-менеджеру."""
    name: str                      # `shellcheck` — имя бинаря, проверяемое через `command -v`
    apt: Optional[str] = None      # имя пакета в apt (None → не ставится через apt)
    dnf: Optional[str] = None      # dnf/yum
    apk: Optional[str] = None      # alpine
    pacman: Optional[str] = None   # arch
    reason: str = ""               # зачем оно admin-у (для UX)


# Базовые CLI — без них multi-step runbook'и и static validation деградируют.
PREREQS_REQUIRED: Tuple[PrereqTool, ...] = (
    PrereqTool(
        name="bash",
        apt="bash", dnf="bash", apk="bash", pacman="bash",
        reason="runbook execution uses `bash -c`",
    ),
    PrereqTool(
        name="awk",
        apt="gawk", dnf="gawk", apk="gawk", pacman="gawk",
        reason="scan output parsing",
    ),
    PrereqTool(
        name="ss",
        apt="iproute2", dnf="iproute", apk="iproute2", pacman="iproute2",
        reason="network.listen baseline check",
    ),
)

# Дополнительные CLI — admin без них работает, но диагностика беднее.
PREREQS_RECOMMENDED: Tuple[PrereqTool, ...] = (
    PrereqTool(
        name="shellcheck",
        apt="shellcheck", dnf="ShellCheck", apk="shellcheck", pacman="shellcheck",
        reason="static analysis of runbook scripts before execution",
    ),
    PrereqTool(
        name="jq",
        apt="jq", dnf="jq", apk="jq", pacman="jq",
        reason="JSON parsing in custom diagnostic scripts",
    ),
    PrereqTool(
        name="lsof",
        apt="lsof", dnf="lsof", apk="lsof", pacman="lsof",
        reason="open-files inspection for service drift",
    ),
    PrereqTool(
        name="curl",
        apt="curl", dnf="curl", apk="curl", pacman="curl",
        reason="health-check probes in runbooks",
    ),
)

PREREQS_CHECK_ID = "admin.prereqs"


@dataclass
class PrereqsReport:
    ok: bool
    required_missing: List[str] = field(default_factory=list)
    recommended_missing: List[str] = field(default_factory=list)
    distro_id: Optional[str] = None
    pkg_manager: Optional[str] = None
    installable: List[str] = field(default_factory=list)  # пакеты (не CLI-имена)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "required_missing": list(self.required_missing),
            "recommended_missing": list(self.recommended_missing),
            "distro_id": self.distro_id,
            "pkg_manager": self.pkg_manager,
            "installable": list(self.installable),
        }


# --- BaselineCheck integration -------------------------------------------------

def _all_tool_names() -> List[str]:
    names: List[str] = []
    for t in (*PREREQS_REQUIRED, *PREREQS_RECOMMENDED):
        if t.name not in names:
            names.append(t.name)
    return names


def prereqs_command(tools: Optional[Sequence[str]] = None) -> str:
    """
    Shell-команда, проверяющая наличие каждого CLI через `command -v`.
    Вывод: по строке на tool: `<name> ok` или `<name> missing`.
    """
    names = list(tools) if tools is not None else _all_tool_names()
    # одна строка — безопасно для bash -lc
    parts: List[str] = []
    for n in names:
        safe = n.replace('"', "").replace("'", "").replace("$", "").replace("`", "")
        if not safe:
            continue
        parts.append(
            f'if command -v "{safe}" >/dev/null 2>&1; '
            f'then echo "{safe} ok"; else echo "{safe} missing"; fi'
        )
    return "; ".join(parts) if parts else "true"


def parse_prereqs_output(text: str) -> Dict[str, bool]:
    """Парсит вывод prereqs_command в dict: tool_name → present?"""
    out: Dict[str, bool] = {}
    for line in (text or "").splitlines():
        parts = line.strip().split()
        if len(parts) != 2:
            continue
        name, state = parts[0], parts[1].lower()
        if state not in ("ok", "missing"):
            continue
        out[name] = (state == "ok")
    return out


# --- evaluation ---------------------------------------------------------------

_DISTRO_TO_PM: Dict[str, str] = {
    "ubuntu": "apt", "debian": "apt", "linuxmint": "apt", "pop": "apt",
    "fedora": "dnf", "rhel": "dnf", "centos": "dnf", "rocky": "dnf", "almalinux": "dnf",
    "alpine": "apk",
    "arch": "pacman", "manjaro": "pacman",
}


def _pkg_manager_for(distro_id: Optional[str], id_like: Optional[str] = None) -> Optional[str]:
    if distro_id:
        pm = _DISTRO_TO_PM.get(str(distro_id).strip().lower())
        if pm:
            return pm
    if id_like:
        for token in str(id_like).replace(",", " ").split():
            pm = _DISTRO_TO_PM.get(token.strip().lower())
            if pm:
                return pm
    return None


def _pkg_name(tool: PrereqTool, pm: Optional[str]) -> Optional[str]:
    if pm == "apt":
        return tool.apt
    if pm == "dnf":
        return tool.dnf
    if pm == "apk":
        return tool.apk
    if pm == "pacman":
        return tool.pacman
    return None


def evaluate_prereqs(
    presence: Mapping[str, bool],
    *,
    distro_id: Optional[str] = None,
    id_like: Optional[str] = None,
) -> PrereqsReport:
    """По результату check + info о distro составить PrereqsReport."""
    required_missing = [t.name for t in PREREQS_REQUIRED if not presence.get(t.name, False)]
    recommended_missing = [t.name for t in PREREQS_RECOMMENDED if not presence.get(t.name, False)]

    pm = _pkg_manager_for(distro_id, id_like)
    installable: List[str] = []
    for tool_name in required_missing + recommended_missing:
        tool = next(
            (t for t in (*PREREQS_REQUIRED, *PREREQS_RECOMMENDED) if t.name == tool_name),
            None,
        )
        if tool is None:
            continue
        pkg = _pkg_name(tool, pm)
        if pkg and pkg not in installable:
            installable.append(pkg)

    return PrereqsReport(
        ok=not required_missing,
        required_missing=required_missing,
        recommended_missing=recommended_missing,
        distro_id=str(distro_id).lower() if distro_id else None,
        pkg_manager=pm,
        installable=installable,
    )


# --- bootstrap script generation ----------------------------------------------

def generate_bootstrap_script(report: PrereqsReport) -> str:
    """
    Идемпотентный shell-скрипт, устанавливающий missing пакеты через детектированный
    package manager. Скрипт консервативный: set -euo pipefail, sudo если не root,
    apt-get update только для apt, никаких upgrade/autoremove.
    """
    missing_any = bool(report.required_missing or report.recommended_missing)
    if not missing_any:
        return (
            "#!/bin/bash\n"
            "# admin prereqs bootstrap — nothing to install\n"
            "set -euo pipefail\n"
            'echo "admin prereqs: all required/recommended tools already installed"\n'
        )
    if not report.installable:
        # Есть missing-инструменты, но package manager не определён (distro неизвестен
        # или не поддерживается). Выдаём fallback, чтобы оператор поставил вручную.
        return (
            "#!/bin/bash\n"
            "# admin prereqs bootstrap — unsupported package manager\n"
            "set -euo pipefail\n"
            f'echo "missing tools: {" ".join(report.required_missing + report.recommended_missing)}"\n'
            'echo "install manually for this distro"\n'
            "exit 1\n"
        )

    pm = report.pkg_manager
    pkgs = " ".join(report.installable)
    prelude = (
        "#!/bin/bash\n"
        f"# admin prereqs bootstrap ({pm or 'unknown pm'}) — generated, manual-only\n"
        "set -euo pipefail\n"
        'if [ "$(id -u)" -ne 0 ]; then\n'
        '  SUDO="sudo"\n'
        "else\n"
        '  SUDO=""\n'
        "fi\n"
    )
    if pm == "apt":
        body = (
            '$SUDO apt-get update -y\n'
            'DEBIAN_FRONTEND=noninteractive $SUDO apt-get install -y --no-install-recommends '
            f'{pkgs}\n'
        )
    elif pm == "dnf":
        body = f"$SUDO dnf install -y {pkgs}\n"
    elif pm == "apk":
        body = f"$SUDO apk add --no-cache {pkgs}\n"
    elif pm == "pacman":
        body = f"$SUDO pacman -Sy --noconfirm --needed {pkgs}\n"
    else:
        return (
            "#!/bin/bash\n"
            "# admin prereqs bootstrap — unsupported package manager\n"
            "set -euo pipefail\n"
            f'echo "missing tools: {" ".join(report.required_missing + report.recommended_missing)}"\n'
            'echo "install manually for this distro"\n'
            "exit 1\n"
        )
    return prelude + body + 'echo "admin prereqs bootstrap done"\n'


__all__ = [
    "PREREQS_CHECK_ID",
    "PREREQS_RECOMMENDED",
    "PREREQS_REQUIRED",
    "PrereqTool",
    "PrereqsReport",
    "evaluate_prereqs",
    "generate_bootstrap_script",
    "parse_prereqs_output",
    "prereqs_command",
]
