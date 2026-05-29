from __future__ import annotations

import logging
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional

import yaml

from .prereqs import parse_prereqs_output as _parse_prereqs_output
from .prereqs import prereqs_command as _prereqs_command
from .snapshot_store import server_dir

try:
    from .transports import (
        LocalCommandSpec,
        LocalCommandResult,
        LocalSubprocessTransport,
        SSHCommandSpec,
        SSHCommandResult,
        SSHSubprocessTransport,
    )
except Exception:  # pragma: no cover
    LocalCommandSpec = None  # type: ignore[assignment]
    LocalSubprocessTransport = None  # type: ignore[assignment]
    SSHCommandSpec = None  # type: ignore[assignment]
    SSHSubprocessTransport = None  # type: ignore[assignment]

_log = logging.getLogger(__name__)

BASELINE_SCHEMA_VERSION = 1
DEFAULT_CHECK_TIMEOUT = 15.0


class BaselineError(RuntimeError):
    """Raised when baseline scan/load fails."""


@dataclass(frozen=True)
class BaselineCheck:
    id: str
    command: str
    parser: Callable[[str], Any]
    label: str = ""
    timeout_sec: float = DEFAULT_CHECK_TIMEOUT


@dataclass
class ServerSpec:
    server_id: str
    transport: str = "local"  # "local" | "ssh"
    host: Optional[str] = None
    user: Optional[str] = None
    port: int = 22
    key_path: Optional[str] = None
    password_env: Optional[str] = None
    ssh_options: tuple = ()
    label: Optional[str] = None
    tags: List[str] = field(default_factory=list)


# --- parsers ---

def _parse_line(text: str) -> str:
    return text.strip().splitlines()[0].strip() if text.strip() else ""


def _parse_lines_sorted_unique(text: str) -> List[str]:
    items = [line.strip() for line in (text or "").splitlines() if line.strip()]
    return sorted(set(items))


def _parse_key_value_file(text: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _parse_systemd_active(text: str) -> List[str]:
    units: List[str] = []
    for line in (text or "").splitlines():
        parts = line.strip().split()
        if not parts:
            continue
        unit = parts[0]
        if unit.endswith(".service"):
            units.append(unit)
    return sorted(set(units))


def _parse_listen_sockets(text: str) -> List[str]:
    out: List[str] = []
    for line in (text or "").splitlines():
        parts = line.strip().split()
        if len(parts) < 5:
            continue
        if parts[0] == "State":
            continue
        local = parts[3] if len(parts) > 3 else ""
        if local:
            out.append(local)
    return sorted(set(out))


def _parse_mounts(text: str) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    for line in (text or "").splitlines():
        parts = line.strip().split()
        if len(parts) < 6:
            continue
        rows.append({"src": parts[0], "target": parts[2], "type": parts[4]})
    rows.sort(key=lambda r: r.get("target", ""))
    return rows


def _parse_disk_space(text: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for line in (text or "").splitlines()[1:]:
        parts = line.strip().split()
        if len(parts) < 2:
            continue
        target = parts[-1]
        pct = parts[-2] if len(parts) >= 2 else ""
        if target and pct.endswith("%"):
            out[target] = pct
    return dict(sorted(out.items()))


def _parse_users(text: str) -> List[str]:
    users: List[str] = []
    for line in (text or "").splitlines():
        parts = line.split(":")
        if len(parts) < 3:
            continue
        try:
            uid = int(parts[2])
        except Exception:
            continue
        if uid >= 1000 and uid < 65000 and parts[0]:
            users.append(parts[0])
    return sorted(set(users))


def _parse_packages(text: str) -> Dict[str, str]:
    pkgs: Dict[str, str] = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if "=" in line:
            k, _, v = line.partition("=")
        elif " " in line:
            parts = line.split()
            k, v = parts[0], parts[-1] if len(parts) > 1 else ""
        else:
            k, v = line, ""
        pkgs[k.strip()] = v.strip()
    return dict(sorted(pkgs.items()))


def _parse_text_block(text: str) -> str:
    return (text or "").strip()


# --- default check set ---

def default_checks() -> List[BaselineCheck]:
    return [
        BaselineCheck(id="os.kernel", label="Kernel version", command="uname -r", parser=_parse_line),
        BaselineCheck(id="os.hostname", label="Hostname", command="hostname", parser=_parse_line),
        BaselineCheck(
            id="os.os_release",
            label="OS release info",
            command="cat /etc/os-release 2>/dev/null || true",
            parser=_parse_key_value_file,
        ),
        BaselineCheck(
            id="systemd.running",
            label="Running systemd services",
            command=(
                "systemctl list-units --type=service --state=running "
                "--no-legend --no-pager 2>/dev/null || true"
            ),
            parser=_parse_systemd_active,
        ),
        BaselineCheck(
            id="network.listen",
            label="Listening TCP/UDP sockets",
            command="ss -Hltnup 2>/dev/null || ss -tlnu 2>/dev/null || true",
            parser=_parse_listen_sockets,
        ),
        BaselineCheck(
            id="mounts",
            label="Mounted filesystems",
            command="mount 2>/dev/null",
            parser=_parse_mounts,
        ),
        BaselineCheck(
            id="disk.space",
            label="Disk usage by mount point",
            command="df -P -h 2>/dev/null",
            parser=_parse_disk_space,
        ),
        BaselineCheck(
            id="users.regular",
            label="Regular users (uid >= 1000)",
            command="getent passwd 2>/dev/null",
            parser=_parse_users,
        ),
        BaselineCheck(
            id="packages.sample",
            label="Installed packages (sample)",
            command=(
                "(dpkg-query -W -f='${Package}=${Version}\\n' 2>/dev/null "
                "|| rpm -qa --qf '%{NAME}=%{VERSION}\\n' 2>/dev/null || true) "
                "| sort | head -n 100"
            ),
            parser=_parse_packages,
        ),
        BaselineCheck(
            id="crontab.root",
            label="Root crontab",
            command="crontab -l 2>/dev/null || true",
            parser=_parse_text_block,
        ),
        BaselineCheck(
            id="admin.prereqs",
            label="Admin-mode CLI prereqs (required + recommended)",
            command=_prereqs_command(),
            parser=_parse_prereqs_output,
        ),
    ]


# --- scanner ---

class AdminBaselineScanner:
    def __init__(
        self,
        *,
        checks: Optional[List[BaselineCheck]] = None,
        local_transport: Optional[Any] = None,
        ssh_transport: Optional[Any] = None,
        secrets_workdir: str = "",
    ) -> None:
        self._checks = list(checks or default_checks())
        self._local = local_transport or (LocalSubprocessTransport() if LocalSubprocessTransport else None)
        self._ssh = ssh_transport or (SSHSubprocessTransport() if SSHSubprocessTransport else None)
        self._secrets_workdir = str(secrets_workdir or "").strip()

    @property
    def checks(self) -> List[BaselineCheck]:
        return list(self._checks)

    async def scan(self, server: ServerSpec) -> Dict[str, Any]:
        results: Dict[str, Any] = {}
        errors: Dict[str, str] = {}
        for check in self._checks:
            try:
                output = await self._run_check(server, check)
                results[check.id] = check.parser(output)
            except Exception as exc:
                _log.warning(
                    "baseline scan: check %s failed for %s: %s",
                    check.id, server.server_id, exc,
                )
                errors[check.id] = str(exc)
                results[check.id] = None

        profile: Dict[str, Any] = {
            "schema_version": BASELINE_SCHEMA_VERSION,
            "server_id": server.server_id,
            "transport": server.transport,
            "host": server.host,
            "user": server.user,
            "port": server.port if server.transport == "ssh" else None,
            "label": server.label or server.server_id,
            "tags": list(server.tags),
            "scanned_at": _now_iso(),
            "checks": results,
        }
        if errors:
            profile["errors"] = errors
        return profile

    async def _run_check(self, server: ServerSpec, check: BaselineCheck) -> str:
        argv = ("bash", "-lc", check.command)
        if server.transport == "local":
            if self._local is None:
                raise BaselineError("local transport is not available")
            spec = LocalCommandSpec(
                action_id=f"baseline:{check.id}",
                argv=argv,
                timeout_sec=float(check.timeout_sec or DEFAULT_CHECK_TIMEOUT),
            )
            res: LocalCommandResult = await self._local.run(spec)
            if res.timed_out:
                raise BaselineError(f"check {check.id} timed out")
            return res.stdout or ""
        if server.transport == "ssh":
            if self._ssh is None:
                raise BaselineError("ssh transport is not available")
            password = ""
            password_env = str(server.password_env or "").strip()
            if password_env:
                from app.services.ssh_config_loader import load_ssh_secrets, resolve_ssh_secret

                password = str(resolve_ssh_secret(load_ssh_secrets(self._secrets_workdir), password_env) or "")
            if not server.host or (not server.key_path and not password):
                raise BaselineError("ssh server requires host and key_path/password_env")
            spec = SSHCommandSpec(
                action_id=f"baseline:{check.id}",
                host=server.host,
                argv=argv,
                key_path=server.key_path or "",
                user=server.user,
                port=int(server.port or 22),
                timeout_sec=float(check.timeout_sec or DEFAULT_CHECK_TIMEOUT),
                options=tuple(server.ssh_options or ()),
                password=password or None,
            )
            res: SSHCommandResult = await self._ssh.run(spec)
            if res.timed_out:
                raise BaselineError(f"check {check.id} timed out")
            return res.stdout or ""
        raise BaselineError(f"unknown transport: {server.transport!r}")


# --- file I/O ---

def baseline_path(workdir: str, server_id: str) -> Path:
    return server_dir(workdir, server_id) / "baseline.yaml"


def proposed_baseline_path(workdir: str, server_id: str) -> Path:
    return server_dir(workdir, server_id) / "baseline.proposed.yaml"


def prev_baseline_path(workdir: str, server_id: str) -> Path:
    return server_dir(workdir, server_id) / "baseline.prev.yaml"


def load_baseline(workdir: str, server_id: str) -> Optional[Dict[str, Any]]:
    path = baseline_path(workdir, server_id)
    if not path.is_file():
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise BaselineError(f"failed to parse {path}: {exc}") from exc
    if not isinstance(data, Mapping):
        raise BaselineError(f"baseline file {path} is not a mapping")
    return dict(data)


def load_proposed_baseline(workdir: str, server_id: str) -> Optional[Dict[str, Any]]:
    path = proposed_baseline_path(workdir, server_id)
    if not path.is_file():
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise BaselineError(f"failed to parse {path}: {exc}") from exc
    if not isinstance(data, Mapping):
        raise BaselineError(f"proposed baseline file {path} is not a mapping")
    return dict(data)


def write_baseline(workdir: str, server_id: str, profile: Mapping[str, Any]) -> Path:
    path = baseline_path(workdir, server_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    _dump_yaml(path, dict(profile))
    return path


def write_proposed_baseline(workdir: str, server_id: str, profile: Mapping[str, Any]) -> Path:
    path = proposed_baseline_path(workdir, server_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    _dump_yaml(path, dict(profile))
    return path


def apply_scan_result(
    workdir: str,
    server_id: str,
    profile: Mapping[str, Any],
) -> Dict[str, Any]:
    """
    Политика обновления baseline:
    - Если baseline.yaml нет — создаём (первый скан).
    - Если есть — кладём в baseline.proposed.yaml для ручного accept.
    Возвращает dict c path'ами и действием.
    """
    existing = load_baseline(workdir, server_id)
    if existing is None:
        path = write_baseline(workdir, server_id, profile)
        return {"action": "created", "path": str(path)}
    path = write_proposed_baseline(workdir, server_id, profile)
    return {"action": "proposed", "path": str(path)}


def accept_proposed_baseline(workdir: str, server_id: str) -> Dict[str, Any]:
    """Переносит proposed → baseline, старый baseline → baseline.prev."""
    proposed = proposed_baseline_path(workdir, server_id)
    if not proposed.is_file():
        raise BaselineError("no proposed baseline to accept")
    target = baseline_path(workdir, server_id)
    if target.is_file():
        shutil.copy2(target, prev_baseline_path(workdir, server_id))
    shutil.move(str(proposed), str(target))
    return {"accepted": str(target), "prev": str(prev_baseline_path(workdir, server_id))}


def discard_proposed_baseline(workdir: str, server_id: str) -> bool:
    path = proposed_baseline_path(workdir, server_id)
    if not path.is_file():
        return False
    path.unlink()
    return True


def _dump_yaml(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(
            data,
            fh,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
            indent=2,
        )
    os.replace(tmp, path)


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


__all__ = [
    "AdminBaselineScanner",
    "BaselineCheck",
    "BaselineError",
    "ServerSpec",
    "accept_proposed_baseline",
    "apply_scan_result",
    "baseline_path",
    "default_checks",
    "discard_proposed_baseline",
    "load_baseline",
    "load_proposed_baseline",
    "prev_baseline_path",
    "proposed_baseline_path",
    "write_baseline",
    "write_proposed_baseline",
]
