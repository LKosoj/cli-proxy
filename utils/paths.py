from __future__ import annotations

import os

from sessions.scoped_key import sanitize_scoped_key_token


def sandbox_root(workdir: str) -> str:
    return os.path.join(workdir, "_sandbox")


def sandbox_shared_dir(workdir: str) -> str:
    return os.path.join(sandbox_root(workdir), "_shared")


def sandbox_session_dir(workdir: str, session_scoped_key: str) -> str:
    token = sanitize_scoped_key_token(session_scoped_key) or "default"
    return os.path.join(sandbox_root(workdir), "sessions", token)


def legacy_sandbox_session_dir(workdir: str, session_id: str) -> str:
    token = sanitize_scoped_key_token(session_id) or "default"
    return os.path.join(sandbox_root(workdir), "sessions", token)


def cli_proxy_root(workdir: str) -> str:
    return os.path.join(str(workdir or ""), ".cli-proxy")


def cli_proxy_artifact_path(workdir: str, artifact: str) -> str:
    name = str(artifact or "").strip().replace("\\", "/").lstrip("/")
    return os.path.join(cli_proxy_root(workdir), name)


def is_within_root(path: str, root: str) -> bool:
    try:
        real_path = os.path.realpath(path)
        real_root = os.path.realpath(root)
        return os.path.commonpath([real_path, real_root]) == real_root
    except Exception:
        return False
