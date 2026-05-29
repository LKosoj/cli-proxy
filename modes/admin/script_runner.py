"""
Исполнитель script-based runbook'ов.

Выполняет один step из runbook'а (frontmatter.steps[*]) на target-сервере:
    - target_hint='local' → LocalSubprocessTransport (хост бота)
    - target_hint='ssh'   → SSHSubprocessTransport (по host/key_path из admin.servers.<id>)

Безопасность:
    - Скрипт читается только из scripts_dir(workdir, rb_id) (resolve + relative_to).
    - Symlinks запрещены.
    - SHA1 проверяется против checksum из frontmatter (если verify_checksum=True).
    - server_id должен быть в rb.servers (fnmatch) — promote gate.

Запуск: body передаётся через `bash -c <body>`. Это даёт единый argv и для local, и для ssh,
и не требует scp/stdin-copy. Ограничение на размер body — MAX_SCRIPT_BYTES.
"""

from __future__ import annotations

import hashlib
import logging
import os
import stat as stat_mod
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

from .config_store import AdminConfigStore
from .memory import ServerMemory
from .runbook_builder import scripts_dir
from .runbooks import Runbook, load_runbooks
from .snapshot_store import safe_server_id
from .transports import (
    LocalCommandSpec,
    LocalSubprocessTransport,
    SSHCommandSpec,
    SSHSubprocessTransport,
)

_log = logging.getLogger(__name__)

MAX_SCRIPT_BYTES = 128 * 1024
DEFAULT_TIMEOUT_SEC = 120.0


class ScriptRunnerError(RuntimeError):
    """Raised when script step cannot be prepared or executed."""


@dataclass
class ScriptRunResult:
    success: bool
    rb_id: str
    step: str
    script: str
    server_id: str
    target: str
    dry_run: bool
    applied: bool
    checksum_ok: Optional[bool]
    exit_code: Optional[int]
    stdout: str
    stderr: str
    duration_ms: int
    command_preview: str
    timed_out: bool = False
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "rb_id": self.rb_id,
            "step": self.step,
            "script": self.script,
            "server_id": self.server_id,
            "target": self.target,
            "dry_run": self.dry_run,
            "applied": self.applied,
            "checksum_ok": self.checksum_ok,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration_ms": self.duration_ms,
            "command_preview": self.command_preview,
            "timed_out": self.timed_out,
            "error": self.error,
        }


def _find_runbook(workdir: str, rb_id: str) -> Runbook:
    for rb in load_runbooks(workdir):
        if rb.id == rb_id:
            return rb
    raise ScriptRunnerError(f"runbook not found: {rb_id}")


def _check_server_authorized(rb: Runbook, sid: str) -> None:
    if not rb.servers:
        raise ScriptRunnerError(
            f"runbook {rb.id} has empty servers list — cannot execute (promote first)",
        )
    # Для script.run требуем точный match. fnmatch с wildcards запрещён:
    # пустой шаблон `*` в servers мог бы случайно авторизовать любой server_id.
    for pattern in rb.servers:
        p = str(pattern)
        if any(ch in p for ch in "*?["):
            continue
        if p == sid:
            return
    raise ScriptRunnerError(
        f"server {sid} is not in runbook.servers {rb.servers!r} — run promote_runbook first",
    )


def _find_step(rb: Runbook, step_name: str) -> Dict[str, Any]:
    steps = rb.metadata.get("steps") if isinstance(rb.metadata, Mapping) else None
    if not isinstance(steps, list):
        raise ScriptRunnerError(f"runbook {rb.id} has no steps")
    name = str(step_name or "").strip()
    if not name:
        raise ScriptRunnerError("step_name is empty")
    for raw in steps:
        if isinstance(raw, Mapping) and str(raw.get("name") or "").strip() == name:
            return dict(raw)
    raise ScriptRunnerError(f"step {name!r} not found in runbook {rb.id}")


def _resolve_script_path(workdir: str, rb_id: str, script_name: str) -> Path:
    sdir = scripts_dir(workdir, rb_id)
    if not sdir.is_dir():
        raise ScriptRunnerError(f"scripts directory missing: {sdir}")
    script_path = sdir / script_name
    try:
        resolved = script_path.resolve(strict=True)
    except FileNotFoundError as exc:
        raise ScriptRunnerError(f"script missing: {script_path}") from exc
    if os.path.islink(script_path):
        raise ScriptRunnerError(f"script is symlink (not allowed): {script_path}")
    try:
        resolved.relative_to(sdir.resolve())
    except ValueError as exc:
        raise ScriptRunnerError(f"script path escapes scripts dir: {resolved}") from exc
    return resolved


def _open_script_nofollow(path: Path) -> int:
    """Открывает скрипт с O_NOFOLLOW — защита от TOCTOU-подмены файла на симлинк."""
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    return os.open(str(path), flags)


def _read_script_via_fd(path: Path, max_bytes: int) -> Tuple[bytes, str]:
    """Открывает fd с O_NOFOLLOW, читает bytes и SHA1 из одного fd — без TOCTOU."""
    try:
        fd = _open_script_nofollow(path)
    except OSError as exc:
        raise ScriptRunnerError(f"cannot open script {path}: {exc}") from exc
    try:
        st = os.fstat(fd)
        if not stat_mod.S_ISREG(st.st_mode):
            raise ScriptRunnerError(f"script is not a regular file: {path}")
        size = int(st.st_size)
        if size > int(max_bytes):
            raise ScriptRunnerError(
                f"script exceeds MAX_SCRIPT_BYTES={max_bytes} (got {size})",
            )
        chunks: list[bytes] = []
        while True:
            data = os.read(fd, 65536)
            if not data:
                break
            chunks.append(data)
        body = b"".join(chunks)
        h = hashlib.sha1()
        h.update(body)
        return body, h.hexdigest()
    finally:
        os.close(fd)


def _parse_checksum(raw: Any) -> Optional[str]:
    s = str(raw or "").strip()
    if not s:
        return None
    if ":" not in s:
        return s
    algo, digest = s.split(":", 1)
    if algo.lower() != "sha1":
        return None
    return digest.strip() or None


def _find_server_cfg(admin_cfg: Mapping[str, Any], sid: str) -> Dict[str, Any]:
    servers = admin_cfg.get("servers") if isinstance(admin_cfg, Mapping) else None
    if isinstance(servers, Mapping) and sid in servers:
        row = servers.get(sid) or {}
        if isinstance(row, Mapping):
            return dict(row)
    if isinstance(servers, list):
        for item in servers:
            if not isinstance(item, Mapping):
                continue
            item_id = str(item.get("id") or item.get("server_id") or "").strip()
            if item_id == sid:
                return dict(item)
    raise ScriptRunnerError(f"server {sid} not found in admin.servers")


def _resolve_key_path(workdir: str, key_path: str) -> str:
    kp = str(key_path or "").strip()
    if not kp:
        raise ScriptRunnerError("ssh server missing key_path")
    if not kp.startswith("/"):
        kp = os.path.abspath(os.path.join(str(workdir or ""), kp))
    return kp


def _audit(workdir: str, sid: str, text: str) -> None:
    try:
        ServerMemory(workdir, sid).append_note(text, source="script_runner")
    except Exception:
        _log.exception("script_runner audit failed server=%s", sid)


async def run_runbook_step(
    *,
    workdir: str,
    rb_id: str,
    step_name: str,
    server_id: str,
    dry_run: bool = True,
    verify_checksum: bool = True,
    timeout_sec: Optional[float] = None,
    admin_cfg: Optional[Mapping[str, Any]] = None,
    local_transport: Optional[LocalSubprocessTransport] = None,
    ssh_transport: Optional[SSHSubprocessTransport] = None,
) -> ScriptRunResult:
    sid = safe_server_id(server_id)
    rb = _find_runbook(workdir, rb_id)
    _check_server_authorized(rb, sid)
    step = _find_step(rb, step_name)

    script_name = str(step.get("script") or "").strip()
    if not script_name:
        raise ScriptRunnerError(f"step {step_name!r} has empty script name")
    target = str(step.get("target_hint") or "local").strip().lower()
    if target not in ("local", "ssh"):
        raise ScriptRunnerError(f"step target_hint invalid: {target!r}")

    script_path = _resolve_script_path(workdir, rb_id, script_name)
    body_bytes, actual_sha1 = _read_script_via_fd(script_path, MAX_SCRIPT_BYTES)
    body_text = body_bytes.decode("utf-8", errors="replace")

    checksum_ok: Optional[bool] = None
    expected = _parse_checksum(step.get("checksum"))
    if verify_checksum:
        if expected is None:
            raise ScriptRunnerError(
                f"step {step_name!r} has no checksum — cannot verify integrity",
            )
        checksum_ok = expected == actual_sha1
        if not checksum_ok:
            _audit(
                workdir, sid,
                f"CHECKSUM-MISMATCH rb={rb_id} step={step_name} script={script_name} "
                f"expected={expected[:8]}… got={actual_sha1[:8]}…",
            )
            raise ScriptRunnerError(
                f"checksum mismatch for {script_name}: expected {expected[:8]}… got {actual_sha1[:8]}…",
            )
    else:
        _audit(
            workdir, sid,
            f"INTEGRITY-OFF rb={rb_id} step={step_name} script={script_name} "
            "verify_checksum=False — SHA1 not enforced",
        )

    argv = ("bash", "-c", body_text)
    timeout = float(timeout_sec) if timeout_sec else DEFAULT_TIMEOUT_SEC
    command_preview = f"bash {script_name}"
    action_id = f"script.run:{rb_id}/{step_name}"

    if dry_run:
        _audit(
            workdir, sid,
            f"DRY-RUN script rb={rb_id} step={step_name} target={target} script={script_name}",
        )
        return ScriptRunResult(
            success=True,
            rb_id=rb_id,
            step=step_name,
            script=script_name,
            server_id=sid,
            target=target,
            dry_run=True,
            applied=False,
            checksum_ok=checksum_ok,
            exit_code=None,
            stdout="",
            stderr="",
            duration_ms=0,
            command_preview=command_preview,
        )

    started_at = time.perf_counter()
    if target == "local":
        transport = local_transport or LocalSubprocessTransport()
        spec = LocalCommandSpec(action_id=action_id, argv=argv, timeout_sec=timeout)
        try:
            res = await transport.run(spec)
        except Exception as exc:
            err = str(exc)
            _audit(workdir, sid, f"ERROR script rb={rb_id} step={step_name} target=local error={err}")
            return ScriptRunResult(
                success=False, rb_id=rb_id, step=step_name, script=script_name,
                server_id=sid, target=target, dry_run=False, applied=False,
                checksum_ok=checksum_ok, exit_code=None, stdout="", stderr="",
                duration_ms=0, command_preview=command_preview, error=err,
            )
        duration_ms = int((time.perf_counter() - started_at) * 1000)
        success = (res.returncode == 0) and not res.timed_out
        _audit(
            workdir, sid,
            f"EXECUTED script rb={rb_id} step={step_name} target=local rc={res.returncode} "
            f"timed_out={res.timed_out} duration_ms={duration_ms}",
        )
        return ScriptRunResult(
            success=success, rb_id=rb_id, step=step_name, script=script_name,
            server_id=sid, target=target, dry_run=False, applied=success,
            checksum_ok=checksum_ok, exit_code=int(res.returncode),
            stdout=res.stdout or "", stderr=res.stderr or "",
            duration_ms=duration_ms, command_preview=command_preview,
            timed_out=bool(res.timed_out),
        )

    # ssh target
    if admin_cfg is None:
        cfg_payload = AdminConfigStore(workdir).load_effective_config()
        admin_cfg = cfg_payload.get("admin") if isinstance(cfg_payload, Mapping) else {}
    server_cfg = _find_server_cfg(admin_cfg or {}, sid)
    if str(server_cfg.get("target") or "").strip().lower() != "ssh":
        raise ScriptRunnerError(
            f"step target_hint=ssh but admin.servers.{sid}.target is not ssh",
        )

    host = str(server_cfg.get("host") or "").strip()
    if not host:
        raise ScriptRunnerError(f"admin.servers.{sid} missing host")
    key_path = _resolve_key_path(workdir, str(server_cfg.get("key_path") or ""))
    user = str(server_cfg.get("user") or "").strip() or None
    port = int(server_cfg.get("port") or 22)
    options = tuple(
        str(opt).strip() for opt in (server_cfg.get("options") or ()) if str(opt).strip()
    )

    transport_ssh = ssh_transport or SSHSubprocessTransport()
    ssh_spec = SSHCommandSpec(
        action_id=action_id,
        host=host,
        argv=argv,
        key_path=key_path,
        user=user,
        port=port,
        timeout_sec=timeout,
        options=options,
    )
    try:
        res = await transport_ssh.run(ssh_spec)
    except Exception as exc:
        err = str(exc)
        _audit(workdir, sid, f"ERROR script rb={rb_id} step={step_name} target=ssh host={host} error={err}")
        return ScriptRunResult(
            success=False, rb_id=rb_id, step=step_name, script=script_name,
            server_id=sid, target=target, dry_run=False, applied=False,
            checksum_ok=checksum_ok, exit_code=None, stdout="", stderr="",
            duration_ms=0, command_preview=command_preview, error=err,
        )
    duration_ms = int((time.perf_counter() - started_at) * 1000)
    success = (res.returncode == 0) and not res.timed_out
    _audit(
        workdir, sid,
        f"EXECUTED script rb={rb_id} step={step_name} target=ssh host={host} "
        f"rc={res.returncode} timed_out={res.timed_out} duration_ms={duration_ms}",
    )
    return ScriptRunResult(
        success=success, rb_id=rb_id, step=step_name, script=script_name,
        server_id=sid, target=target, dry_run=False, applied=success,
        checksum_ok=checksum_ok, exit_code=int(res.returncode),
        stdout=res.stdout or "", stderr=res.stderr or "",
        duration_ms=duration_ms, command_preview=command_preview,
        timed_out=bool(res.timed_out),
    )


__all__ = [
    "DEFAULT_TIMEOUT_SEC",
    "MAX_SCRIPT_BYTES",
    "ScriptRunResult",
    "ScriptRunnerError",
    "run_runbook_step",
]
