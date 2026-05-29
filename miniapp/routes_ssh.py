from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict

from aiohttp import web

from app.services.ssh_config_loader import (
    build_ssh_secret_env_name,
    load_ssh_config,
    save_ssh_config,
    save_ssh_secret,
)
from config import SSHHostConfig

from .route_context import MiniAppRouteContext


RequireAccess = Callable[[web.Request], Awaitable[Dict[str, Any]]]
RequireAdmin = Callable[[web.Request], Awaitable[Dict[str, Any]]]
ReadJsonObject = Callable[[web.Request], Awaitable[Dict[str, Any]]]
JsonError = Callable[[int, Any], Awaitable[web.Response]]


@dataclass(frozen=True)
class SshRouteServices:
    require_access: RequireAccess
    require_admin: RequireAdmin
    read_json_object: ReadJsonObject
    json_error: JsonError


def _resolve_ssh_workdir(request: web.Request) -> str:
    workdir = str(request.query.get("workdir", "") or "").strip()
    if not workdir:
        raise web.HTTPBadRequest(reason="workdir query parameter is required")
    return os.path.abspath(workdir)


def _user_project_roots(ctx: MiniAppRouteContext, user_id: int) -> set[str]:
    getter = getattr(ctx.bot_app, "user_projects", None)
    if callable(getter):
        try:
            return {os.path.realpath(str(path)) for path in getter(int(user_id)) if str(path).strip()}
        except Exception:
            ctx.logger.exception("miniapp ssh user project lookup failed user_id=%s", user_id)
            return set()
    raw = (getattr(getattr(ctx.bot_app.config, "telegram", None), "user_workdirs", {}) or {}).get(int(user_id)) or []
    if isinstance(raw, str):
        raw = [raw]
    return {os.path.realpath(str(path)) for path in raw if str(path).strip()}


def _resolve_ssh_workdir_for_user(
    ctx: MiniAppRouteContext,
    request: web.Request,
    user: Dict[str, Any],
) -> str:
    workdir = _resolve_ssh_workdir(request)
    if bool(user.get("is_admin", False)):
        return workdir
    real_workdir = os.path.realpath(workdir)
    if real_workdir not in _user_project_roots(ctx, int(user["user_id"])):
        raise web.HTTPForbidden(reason="workdir is not allowed for this user")
    return workdir


def _ssh_host_visible_for_user(user: Dict[str, Any], cfg: Any) -> bool:
    if bool(user.get("is_admin", False)):
        return True
    acl = getattr(cfg, "allowed_chat_ids", None)
    if acl is None:
        return True
    return int(user["user_id"]) in {int(item) for item in acl}


def _ssh_host_payload(cfg: Any) -> Dict[str, Any]:
    return {
        "host": cfg.host,
        "port": cfg.port,
        "user": cfg.user,
        "auth": cfg.auth,
        "sudo": cfg.sudo,
        "idle_timeout_sec": cfg.idle_timeout_sec,
        "allowed_chat_ids": cfg.allowed_chat_ids,
        "roles": cfg.roles,
        "description": cfg.description,
        "remote_project_root": cfg.remote_project_root,
        "has_password": cfg.password_env is not None,
        "has_sudo_password": cfg.sudo_password_env is not None,
        "has_key": cfg.key_file is not None,
    }


async def _read_body_or_error(
    services: SshRouteServices,
    request: web.Request,
) -> Dict[str, Any] | web.Response:
    try:
        return await services.read_json_object(request)
    except web.HTTPException as exc:
        return await services.json_error(int(exc.status), str(exc.reason or "invalid request"))


def _invalidate_preflight(ctx: MiniAppRouteContext, workdir: str, alias: str) -> None:
    rc_svc = getattr(ctx.bot_app, "remote_control_service", None)
    if rc_svc is not None:
        rc_svc.invalidate_preflight(workdir, alias)


def _validate_alias(alias: str) -> bool:
    return bool(alias and re.match(r"^[a-zA-Z0-9_-]+$", alias))


def _build_host_config(alias: str, body: Dict[str, Any]) -> SSHHostConfig:
    host = str(body.get("host", "") or "").strip()
    user = str(body.get("user", "") or "").strip()
    if not host or not user:
        raise web.HTTPBadRequest(reason="host and user are required")

    auth = str(body.get("auth", "key") or "key").strip()
    if auth not in ("key", "password"):
        raise web.HTTPBadRequest(reason="auth must be 'key' or 'password'")

    password = str(body.get("password", "") or "").strip()
    sudo_password = str(body.get("sudo_password", "") or "").strip()
    password_env = str(body.get("password_env", "") or "").strip() or None
    sudo_password_env = str(body.get("sudo_password_env", "") or "").strip() or None
    if auth == "password" and password and not password_env:
        password_env = build_ssh_secret_env_name(alias)
    if bool(body.get("sudo", False)) and sudo_password and not sudo_password_env:
        sudo_password_env = build_ssh_secret_env_name(alias, sudo=True)

    remote_project_root = str(body.get("remote_project_root", "") or "").strip() or None
    if remote_project_root and not remote_project_root.startswith("/"):
        raise web.HTTPBadRequest(reason="remote_project_root must be an absolute path")

    return SSHHostConfig(
        host=host,
        user=user,
        auth=auth,
        port=int(body.get("port", 22) or 22),
        key_file=body.get("key_file") or None,
        key_passphrase_env=body.get("key_passphrase_env") or None,
        password_env=password_env,
        sudo=bool(body.get("sudo", False)),
        sudo_password_env=sudo_password_env,
        idle_timeout_sec=int(body.get("idle_timeout_sec", 1200) or 1200),
        allowed_chat_ids=body.get("allowed_chat_ids"),
        roles=list(body.get("roles") or []),
        description=str(body.get("description", "") or ""),
        remote_project_root=remote_project_root,
    )


def _save_optional_secrets(workdir: str, body: Dict[str, Any], cfg: SSHHostConfig) -> None:
    password = str(body.get("password", "") or "").strip()
    sudo_password = str(body.get("sudo_password", "") or "").strip()
    if password and cfg.password_env:
        save_ssh_secret(workdir, cfg.password_env, password)
    if sudo_password and cfg.sudo_password_env:
        save_ssh_secret(workdir, cfg.sudo_password_env, sudo_password)


def register_ssh_routes(
    app: web.Application,
    ctx: MiniAppRouteContext,
    services: SshRouteServices,
) -> None:
    async def ssh_hosts_list(request: web.Request) -> web.Response:
        user = await services.require_access(request)
        workdir = _resolve_ssh_workdir_for_user(ctx, request, user)
        hosts = load_ssh_config(workdir)
        result = {}
        for alias, cfg in hosts.items():
            if not _ssh_host_visible_for_user(user, cfg):
                continue
            result[alias] = _ssh_host_payload(cfg)
        return web.json_response({"ok": True, "hosts": result})

    async def ssh_host_create(request: web.Request) -> web.Response:
        await services.require_admin(request)
        workdir = _resolve_ssh_workdir(request)
        body = await _read_body_or_error(services, request)
        if isinstance(body, web.Response):
            return body

        alias = str(body.get("alias", "") or "").strip()
        if not _validate_alias(alias):
            return await services.json_error(400, "alias must be non-empty alphanumeric with - or _")

        hosts = load_ssh_config(workdir)
        if alias in hosts:
            return await services.json_error(409, f"Host '{alias}' already exists")

        try:
            cfg = _build_host_config(alias, body)
        except web.HTTPBadRequest as exc:
            return await services.json_error(int(exc.status), str(exc.reason or "invalid request"))

        hosts[alias] = cfg
        save_ssh_config(workdir, hosts)
        _invalidate_preflight(ctx, workdir, alias)
        _save_optional_secrets(workdir, body, cfg)
        return web.json_response({"ok": True, "alias": alias})

    async def ssh_host_update(request: web.Request) -> web.Response:
        await services.require_admin(request)
        workdir = _resolve_ssh_workdir(request)
        body = await _read_body_or_error(services, request)
        if isinstance(body, web.Response):
            return body

        alias = str(body.get("alias", "") or "").strip()
        if not alias:
            return await services.json_error(400, "alias is required")

        hosts = load_ssh_config(workdir)
        if alias not in hosts:
            return await services.json_error(404, f"Host '{alias}' not found")

        try:
            cfg = _build_host_config(alias, body)
        except web.HTTPBadRequest as exc:
            return await services.json_error(int(exc.status), str(exc.reason or "invalid request"))

        hosts[alias] = cfg
        save_ssh_config(workdir, hosts)
        _invalidate_preflight(ctx, workdir, alias)
        _save_optional_secrets(workdir, body, cfg)
        return web.json_response({"ok": True, "alias": alias})

    async def ssh_host_delete(request: web.Request) -> web.Response:
        await services.require_admin(request)
        workdir = _resolve_ssh_workdir(request)
        body = await _read_body_or_error(services, request)
        if isinstance(body, web.Response):
            return body

        alias = str(body.get("alias", "") or "").strip()
        if not alias:
            return await services.json_error(400, "alias is required")

        hosts = load_ssh_config(workdir)
        if alias not in hosts:
            return await services.json_error(404, f"Host '{alias}' not found")
        del hosts[alias]
        save_ssh_config(workdir, hosts)
        _invalidate_preflight(ctx, workdir, alias)
        return web.json_response({"ok": True, "alias": alias})

    async def ssh_test_connection(request: web.Request) -> web.Response:
        await services.require_admin(request)
        workdir = _resolve_ssh_workdir(request)
        body = await _read_body_or_error(services, request)
        if isinstance(body, web.Response):
            return body

        alias = str(body.get("alias", "") or "").strip()
        if not alias:
            return await services.json_error(400, "alias is required")

        ssh_service = getattr(ctx.bot_app, "ssh_service", None)
        if ssh_service is None:
            return await services.json_error(503, "SSH service unavailable")

        try:
            result = await ssh_service.test_connection(workdir, alias)
            return web.json_response({
                "ok": result.ok,
                "message": result.message,
                "server_info": result.server_info,
            })
        except Exception as exc:
            return web.json_response({
                "ok": False,
                "message": str(exc),
                "server_info": None,
            })

    async def ssh_keygen(request: web.Request) -> web.Response:
        await services.require_admin(request)
        workdir = _resolve_ssh_workdir(request)
        body = await _read_body_or_error(services, request)
        if isinstance(body, web.Response):
            return body

        alias = str(body.get("alias", "") or "").strip()
        if not alias:
            return await services.json_error(400, "alias is required")

        ssh_service = getattr(ctx.bot_app, "ssh_service", None)
        if ssh_service is None:
            return await services.json_error(503, "SSH service unavailable")

        try:
            result = await ssh_service.generate_key(workdir, alias)
            hosts = load_ssh_config(workdir)
            if alias in hosts:
                hosts[alias].key_file = os.path.relpath(result.private_path, workdir)
                hosts[alias].auth = "key"
                save_ssh_config(workdir, hosts)

            return web.json_response({
                "ok": True,
                "public_key": result.public_key_text,
                "key_path": result.private_path,
            })
        except FileExistsError:
            return await services.json_error(409, f"Key for '{alias}' already exists")
        except Exception as exc:
            ctx.logger.exception("SSH keygen failed")
            return await services.json_error(500, str(exc))

    async def ssh_host_detail(request: web.Request) -> web.Response:
        user = await services.require_access(request)
        workdir = _resolve_ssh_workdir_for_user(ctx, request, user)
        alias = request.match_info.get("alias", "").strip()
        if not alias:
            return await services.json_error(400, "alias is required")
        hosts = load_ssh_config(workdir)
        if alias not in hosts or not _ssh_host_visible_for_user(user, hosts[alias]):
            return await services.json_error(404, f"Host '{alias}' not found")
        return web.json_response({"ok": True, "alias": alias, "host": _ssh_host_payload(hosts[alias])})

    async def ssh_secret_save(request: web.Request) -> web.Response:
        await services.require_admin(request)
        workdir = _resolve_ssh_workdir(request)
        body = await _read_body_or_error(services, request)
        if isinstance(body, web.Response):
            return body
        key = str(body.get("key", "") or "").strip()
        value = str(body.get("value", "") or "")
        if not key:
            return await services.json_error(400, "key is required")
        save_ssh_secret(workdir, key, value)
        return web.json_response({"ok": True, "key": key})

    app.router.add_get("/api/ssh/hosts", ssh_hosts_list)
    app.router.add_get("/api/ssh/hosts/{alias}", ssh_host_detail)
    app.router.add_post("/api/ssh/hosts", ssh_host_create)
    app.router.add_post("/api/ssh/hosts/update", ssh_host_update)
    app.router.add_post("/api/ssh/hosts/delete", ssh_host_delete)
    app.router.add_post("/api/ssh/test-connection", ssh_test_connection)
    app.router.add_post("/api/ssh/keygen", ssh_keygen)
    app.router.add_post("/api/ssh/secret", ssh_secret_save)
