from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional

from modes.sdk.runtime.json_normalizer import loads_safe

from .transports import (
    LocalCommandResult,
    LocalCommandSpec,
    LocalSubprocessTransport,
    LocalTransportError,
    SSHCommandResult,
    SSHCommandSpec,
    SSHSubprocessTransport,
    SSHTransportError,
)

_LOG = logging.getLogger(__name__)
_EXEC_TARGETS = {"local", "ssh"}
_DEFAULT_MAX_CONCURRENT_POLLS = 8


class AdminMonitorError(RuntimeError):
    """Raised when monitor input is invalid."""


@dataclass(frozen=True)
class AdminServerPollSpec:
    server_id: str
    target: str
    action_id: str
    timeout_sec: Optional[float] = None


@dataclass(frozen=True)
class AdminServerSnapshot:
    server_id: str
    target: str
    action_id: str
    ok: bool
    timed_out: bool
    returncode: int
    duration_ms: int
    metrics: Dict[str, Any]
    error: Optional[str]
    collected_at_ts: float


@dataclass(frozen=True)
class AdminMonitorSnapshot:
    created_at_ts: float
    total_servers: int
    ok_servers: int
    failed_servers: int
    servers: tuple[AdminServerSnapshot, ...]


class AdminMonitor:
    def __init__(
        self,
        *,
        local_transport: Optional[LocalSubprocessTransport] = None,
        ssh_transport: Optional[SSHSubprocessTransport] = None,
        max_concurrent_polls: int = _DEFAULT_MAX_CONCURRENT_POLLS,
    ) -> None:
        self._local_transport = local_transport or LocalSubprocessTransport()
        self._ssh_transport = ssh_transport or SSHSubprocessTransport()
        self._max_concurrent_polls = max(1, int(max_concurrent_polls))

    async def collect_snapshot(
        self,
        *,
        config_payload: Mapping[str, Any],
        session_workdir: str = "",
    ) -> AdminMonitorSnapshot:
        poll_specs = self._parse_monitor_specs(config_payload=config_payload)
        created_at_ts = float(time.time())
        if not poll_specs:
            return AdminMonitorSnapshot(
                created_at_ts=created_at_ts,
                total_servers=0,
                ok_servers=0,
                failed_servers=0,
                servers=(),
            )

        semaphore = asyncio.Semaphore(min(self._max_concurrent_polls, len(poll_specs)))

        async def _collect_limited(spec: AdminServerPollSpec) -> AdminServerSnapshot:
            async with semaphore:
                return await self._collect_server_snapshot(
                    spec=spec,
                    config_payload=config_payload,
                    session_workdir=session_workdir,
                )

        tasks = [_collect_limited(spec) for spec in poll_specs]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        servers: list[AdminServerSnapshot] = []
        for idx, result in enumerate(results):
            if isinstance(result, Exception):
                failing_spec = poll_specs[idx]
                _LOG.exception(
                    "admin monitor unexpected failure server_id=%s target=%s action_id=%s",
                    failing_spec.server_id,
                    failing_spec.target,
                    failing_spec.action_id,
                )
                servers.append(
                    AdminServerSnapshot(
                        server_id=failing_spec.server_id,
                        target=failing_spec.target,
                        action_id=failing_spec.action_id,
                        ok=False,
                        timed_out=False,
                        returncode=-1,
                        duration_ms=0,
                        metrics={},
                        error=str(result),
                        collected_at_ts=float(time.time()),
                    )
                )
                continue
            servers.append(result)

        ok_servers = sum(1 for item in servers if item.ok)
        failed_servers = len(servers) - ok_servers
        return AdminMonitorSnapshot(
            created_at_ts=created_at_ts,
            total_servers=len(servers),
            ok_servers=ok_servers,
            failed_servers=failed_servers,
            servers=tuple(servers),
        )

    async def _collect_server_snapshot(
        self,
        *,
        spec: AdminServerPollSpec,
        config_payload: Mapping[str, Any],
        session_workdir: str,
    ) -> AdminServerSnapshot:
        collected_at_ts = float(time.time())
        try:
            action_payload = self._resolve_exec_action_payload(
                config_payload=config_payload,
                target=spec.target,
                action_id=spec.action_id,
            )
            if action_payload is None:
                raise AdminMonitorError(
                    f"action `{spec.action_id}` is missing for target `{spec.target}`"
                )

            if spec.target == "local":
                command_spec = self._build_local_command_spec(
                    action_id=spec.action_id,
                    action_payload=action_payload,
                    session_workdir=session_workdir,
                    timeout_override_sec=spec.timeout_sec,
                )
                result = await self._local_transport.run(command_spec)
                return self._snapshot_from_local_result(
                    server_id=spec.server_id,
                    result=result,
                    collected_at_ts=collected_at_ts,
                )

            command_spec = self._build_ssh_command_spec(
                action_id=spec.action_id,
                action_payload=action_payload,
                server_id=spec.server_id,
                session_workdir=session_workdir,
                timeout_override_sec=spec.timeout_sec,
            )
            result = await self._ssh_transport.run(command_spec)
            return self._snapshot_from_ssh_result(
                server_id=spec.server_id,
                result=result,
                collected_at_ts=collected_at_ts,
            )
        except (AdminMonitorError, LocalTransportError, SSHTransportError, ValueError) as exc:
            _LOG.exception(
                "admin monitor poll failed server_id=%s target=%s action_id=%s",
                spec.server_id,
                spec.target,
                spec.action_id,
            )
            return AdminServerSnapshot(
                server_id=spec.server_id,
                target=spec.target,
                action_id=spec.action_id,
                ok=False,
                timed_out=False,
                returncode=-1,
                duration_ms=0,
                metrics={},
                error=str(exc),
                collected_at_ts=collected_at_ts,
            )

    def _parse_monitor_specs(self, *, config_payload: Mapping[str, Any]) -> list[AdminServerPollSpec]:
        admin_cfg = config_payload.get("admin", {}) if isinstance(config_payload, Mapping) else {}
        if not isinstance(admin_cfg, Mapping):
            return []
        monitor_cfg = admin_cfg.get("monitor", {})
        if not isinstance(monitor_cfg, Mapping):
            return []
        raw_servers = monitor_cfg.get("servers", [])
        if not isinstance(raw_servers, list):
            return []

        specs: list[AdminServerPollSpec] = []
        for item in raw_servers:
            if not isinstance(item, Mapping):
                continue
            server_id = str(item.get("id") or item.get("server_id") or "").strip()
            target = str(item.get("target") or "").strip().lower()
            action_id = str(item.get("action_id") or "").strip()
            if not server_id or target not in _EXEC_TARGETS or not action_id:
                continue
            timeout_value = item.get("timeout_sec")
            timeout_sec: Optional[float] = None
            if timeout_value is not None:
                timeout_sec = float(timeout_value)
                if timeout_sec <= 0:
                    continue
            specs.append(
                AdminServerPollSpec(
                    server_id=server_id,
                    target=target,
                    action_id=action_id,
                    timeout_sec=timeout_sec,
                )
            )
        return specs

    @staticmethod
    def _resolve_exec_action_payload(
        *,
        config_payload: Mapping[str, Any],
        target: str,
        action_id: str,
    ) -> Any:
        admin_cfg = config_payload.get("admin", {}) if isinstance(config_payload, Mapping) else {}
        if not isinstance(admin_cfg, Mapping):
            return None

        actions_cfg = admin_cfg.get("actions", {})
        if isinstance(actions_cfg, Mapping):
            actions = actions_cfg.get(str(target), {})
            if isinstance(actions, Mapping) and str(action_id) in actions:
                return actions.get(str(action_id))

        allowlist_cfg = admin_cfg.get("allowlist", {})
        if isinstance(allowlist_cfg, Mapping):
            allowlist = allowlist_cfg.get(str(target), {})
            if isinstance(allowlist, Mapping) and str(action_id) in allowlist:
                return allowlist.get(str(action_id))
        return None

    @staticmethod
    def _build_local_command_spec(
        *,
        action_id: str,
        action_payload: Any,
        session_workdir: str,
        timeout_override_sec: Optional[float],
    ) -> LocalCommandSpec:
        cwd_default = str(session_workdir or "").strip() or None
        argv: list[str]
        cwd = cwd_default
        timeout_sec = 30.0
        env: Optional[Dict[str, str]] = None

        if isinstance(action_payload, (list, tuple)):
            argv = [str(item) for item in action_payload if str(item).strip()]
        elif isinstance(action_payload, Mapping):
            raw_argv = action_payload.get("argv", [])
            if not isinstance(raw_argv, (list, tuple)):
                raise ValueError("local action `argv` must be a list")
            argv = [str(item) for item in raw_argv if str(item).strip()]
            raw_cwd = action_payload.get("cwd")
            if raw_cwd is not None:
                cwd = str(raw_cwd).strip() or cwd_default
            raw_timeout = action_payload.get("timeout_sec")
            if raw_timeout is not None:
                timeout_sec = float(raw_timeout)
            raw_env = action_payload.get("env")
            if raw_env is not None:
                if not isinstance(raw_env, Mapping):
                    raise ValueError("local action `env` must be a mapping")
                env = {str(k): str(v) for k, v in raw_env.items()}
        else:
            raise ValueError("local action payload must be list or mapping")

        if not argv:
            raise ValueError("local action argv is empty")
        if timeout_override_sec is not None:
            timeout_sec = float(timeout_override_sec)
        if timeout_sec <= 0:
            raise ValueError("local action timeout_sec must be > 0")
        if cwd and cwd_default and not cwd.startswith("/"):
            cwd = os.path.abspath(os.path.join(cwd_default, cwd))

        return LocalCommandSpec(
            action_id=str(action_id or "").strip(),
            argv=tuple(argv),
            cwd=cwd,
            timeout_sec=float(timeout_sec),
            env=env,
        )

    @staticmethod
    def _build_ssh_command_spec(
        *,
        action_id: str,
        action_payload: Any,
        server_id: str,
        session_workdir: str,
        timeout_override_sec: Optional[float],
    ) -> SSHCommandSpec:
        if not isinstance(action_payload, Mapping):
            raise ValueError("ssh action payload must be a mapping")

        raw_argv = action_payload.get("argv", [])
        if not isinstance(raw_argv, (list, tuple)):
            raise ValueError("ssh action `argv` must be a list")
        argv = [str(item) for item in raw_argv if str(item).strip()]
        if not argv:
            raise ValueError("ssh action argv is empty")

        host = str(action_payload.get("host") or "").strip()
        key_path = str(action_payload.get("key_path") or "").strip()
        password = str(action_payload.get("password") or "")
        password_env = str(action_payload.get("password_env") or "").strip()
        user = str(action_payload.get("user") or "").strip() or None
        port_raw = action_payload.get("port")
        port = int(port_raw) if port_raw not in (None, "") else 0

        session_workdir_norm = str(session_workdir or "").strip()
        if not host or (not key_path and not password and not password_env):
            alias = str(server_id or "").strip()
            if not alias:
                raise ValueError("ssh action requires server_id to resolve ssh.yaml")
            if not session_workdir_norm:
                raise ValueError("ssh action requires session_workdir to resolve ssh.yaml")
            from app.services.ssh_config_loader import load_ssh_config, load_ssh_secrets, resolve_ssh_secret

            hosts = load_ssh_config(session_workdir_norm)
            resolved = hosts.get(alias)
            if resolved is None:
                raise ValueError(f"ssh action: alias `{alias}` is not defined in ssh.yaml")
            if not host:
                host = str(resolved.host or "").strip()
            if not key_path:
                key_path = str(getattr(resolved, "key_file", None) or "").strip()
            if user is None:
                user = str(resolved.user or "").strip() or None
            if port <= 0:
                port = int(resolved.port or 22)
            if not password_env:
                password_env = str(getattr(resolved, "password_env", None) or "").strip()
            if not password and password_env:
                password = str(resolve_ssh_secret(load_ssh_secrets(session_workdir_norm), password_env) or "")
        elif not password and password_env and session_workdir_norm:
            from app.services.ssh_config_loader import load_ssh_secrets, resolve_ssh_secret

            password = str(resolve_ssh_secret(load_ssh_secrets(session_workdir_norm), password_env) or "")

        if not host:
            raise ValueError("ssh action `host` is required")
        if not key_path and not password:
            raise ValueError("ssh action `key_path` or `password_env` is required")
        if port <= 0:
            port = 22
        if key_path:
            key_path = os.path.expanduser(key_path)
            if session_workdir_norm and not key_path.startswith("/"):
                key_path = os.path.abspath(os.path.join(session_workdir_norm, key_path))

        timeout_sec = float(action_payload.get("timeout_sec") or 30.0)
        if timeout_override_sec is not None:
            timeout_sec = float(timeout_override_sec)
        if timeout_sec <= 0:
            raise ValueError("ssh action timeout_sec must be > 0")

        raw_options = action_payload.get("options") or []
        if not isinstance(raw_options, (list, tuple)):
            raise ValueError("ssh action `options` must be a list")
        options = tuple(str(item).strip() for item in raw_options if str(item).strip())

        return SSHCommandSpec(
            action_id=str(action_id or "").strip(),
            host=host,
            argv=tuple(argv),
            key_path=key_path,
            user=user,
            port=port,
            timeout_sec=timeout_sec,
            options=options,
            password=password or None,
        )

    def _snapshot_from_local_result(
        self,
        *,
        server_id: str,
        result: LocalCommandResult,
        collected_at_ts: float,
    ) -> AdminServerSnapshot:
        return self._build_snapshot(
            server_id=server_id,
            target="local",
            action_id=result.action_id,
            returncode=result.returncode,
            timed_out=result.timed_out,
            duration_ms=result.duration_ms,
            stdout=result.stdout,
            stderr=result.stderr,
            collected_at_ts=collected_at_ts,
        )

    def _snapshot_from_ssh_result(
        self,
        *,
        server_id: str,
        result: SSHCommandResult,
        collected_at_ts: float,
    ) -> AdminServerSnapshot:
        return self._build_snapshot(
            server_id=server_id,
            target="ssh",
            action_id=result.action_id,
            returncode=result.returncode,
            timed_out=result.timed_out,
            duration_ms=result.duration_ms,
            stdout=result.stdout,
            stderr=result.stderr,
            collected_at_ts=collected_at_ts,
        )

    def _build_snapshot(
        self,
        *,
        server_id: str,
        target: str,
        action_id: str,
        returncode: int,
        timed_out: bool,
        duration_ms: int,
        stdout: str,
        stderr: str,
        collected_at_ts: float,
    ) -> AdminServerSnapshot:
        metrics = self._parse_metrics_payload(stdout=stdout, stderr=stderr)
        ok = bool(returncode == 0 and not timed_out)
        error = None
        if not ok:
            err = str(stderr or "").strip()
            error = err or f"returncode={returncode}"
        return AdminServerSnapshot(
            server_id=server_id,
            target=target,
            action_id=action_id,
            ok=ok,
            timed_out=bool(timed_out),
            returncode=int(returncode),
            duration_ms=int(duration_ms),
            metrics=metrics,
            error=error,
            collected_at_ts=float(collected_at_ts),
        )

    @staticmethod
    def _parse_metrics_payload(*, stdout: str, stderr: str) -> Dict[str, Any]:
        out = str(stdout or "").strip()
        err = str(stderr or "").strip()
        if out:
            try:
                parsed = loads_safe(out, strict_first=True)
            except Exception:
                parsed = None
            if isinstance(parsed, Mapping):
                return {str(k): v for k, v in parsed.items()}

            kv_metrics: Dict[str, Any] = {}
            for line in out.splitlines():
                line_clean = str(line or "").strip()
                if not line_clean or "=" not in line_clean:
                    continue
                key, raw_value = line_clean.split("=", 1)
                key = key.strip()
                if not key:
                    continue
                kv_metrics[key] = AdminMonitor._coerce_metric_value(raw_value.strip())
            if kv_metrics:
                return kv_metrics
        if err:
            return {"stderr": err}
        return {}

    @staticmethod
    def _coerce_metric_value(value: str) -> Any:
        raw = str(value or "").strip()
        if not raw:
            return ""
        if raw.lower() in {"true", "false"}:
            return raw.lower() == "true"
        try:
            if "." in raw:
                return float(raw)
            return int(raw)
        except Exception:
            return raw


__all__ = [
    "AdminMonitor",
    "AdminMonitorError",
    "AdminMonitorSnapshot",
    "AdminServerPollSpec",
    "AdminServerSnapshot",
]
