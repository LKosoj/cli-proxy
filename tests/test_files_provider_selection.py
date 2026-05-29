"""Tests for FilesService provider selection based on effective execution target."""

import os
from types import SimpleNamespace

from miniapp.services.files_service import (
    FilesService,
    FilesServiceError,
    LocalFilesProvider,
    RemoteFilesProvider,
)
from app.services.remote_control_service import RemoteControlService
from config import SSHHostConfig
from session import ModeState


def _make_app(tmp_path, *, rc_enabled=False, host_alias=None, remote_root=None):
    """Build a minimal app with session manager and optional remote control."""
    workdir = str(tmp_path)
    os.makedirs(workdir, exist_ok=True)

    session = SimpleNamespace(
        id="s1",
        workdir=workdir,
        modes=ModeState(
            ssh_remote_enabled=rc_enabled,
            remote_control_enabled=rc_enabled,
            remote_control_host_alias=host_alias,
        ),
    )

    manager = SimpleNamespace(
        get_by_uid=lambda uid: session if uid == "1:s1" else None,
    )

    # SSH hosts config
    ssh_hosts = {}
    if host_alias and remote_root:
        from app.services.ssh_config_loader import save_ssh_config
        ssh_hosts[host_alias] = SSHHostConfig(
            host="1.1.1.1", user="u", remote_project_root=remote_root,
        )
        save_ssh_config(workdir, ssh_hosts)

    miniapp_cfg = SimpleNamespace(max_edit_file_size_kb=5120, enable_delete=True)
    config = SimpleNamespace(
        miniapp=miniapp_cfg,
        path=str(tmp_path / "config.yaml"),
    )
    (tmp_path / "config.yaml").write_text("tools: {}")

    security = SimpleNamespace(
        resolve_path=lambda root, rel, **kw: SimpleNamespace(resolved_path=os.path.join(root, rel)),
    )

    app = SimpleNamespace(
        config=config,
        manager=manager,
        security=security,
        remote_control_service=RemoteControlService() if rc_enabled else None,
        ssh_service=SimpleNamespace() if rc_enabled else None,
    )
    return app


class TestProviderSelection:
    """FilesService selects provider based on session effective state."""

    def test_local_when_rc_disabled(self, tmp_path):
        app = _make_app(tmp_path, rc_enabled=False)
        svc = FilesService(app=app)
        provider = svc._provider("1:s1")
        assert isinstance(provider, LocalFilesProvider)

    def test_local_when_no_rc_service(self, tmp_path):
        app = _make_app(tmp_path, rc_enabled=False)
        app.remote_control_service = None
        svc = FilesService(app=app)
        provider = svc._provider("1:s1")
        assert isinstance(provider, LocalFilesProvider)

    def test_remote_when_rc_enabled(self, tmp_path):
        app = _make_app(
            tmp_path, rc_enabled=True,
            host_alias="prod", remote_root="/srv/app",
        )
        svc = FilesService(app=app)
        provider = svc._provider("1:s1")
        assert isinstance(provider, RemoteFilesProvider)
        assert provider.root == "/srv/app"

    def test_remote_provider_uses_ssh_service(self, tmp_path):
        app = _make_app(
            tmp_path, rc_enabled=True,
            host_alias="prod", remote_root="/data",
        )
        ssh_mock = SimpleNamespace(exec=lambda *a, **kw: None)
        app.ssh_service = ssh_mock
        svc = FilesService(app=app)
        provider = svc._provider("1:s1")
        assert isinstance(provider, RemoteFilesProvider)
        assert provider._ssh is ssh_mock

    def test_local_when_rc_enabled_but_alias_missing(self, tmp_path):
        """RC enabled but no host_alias fails closed instead of falling back to local."""
        app = _make_app(tmp_path, rc_enabled=True, host_alias=None)
        svc = FilesService(app=app)
        try:
            svc._provider("1:s1")
        except FilesServiceError as exc:
            assert "remote target is unavailable" in str(exc)
        else:
            raise AssertionError("expected FilesServiceError")

    def test_local_when_rc_enabled_but_host_not_in_config(self, tmp_path):
        """RC enabled with unknown host fails closed instead of falling back to local."""
        app = _make_app(tmp_path, rc_enabled=True, host_alias="missing")
        svc = FilesService(app=app)
        try:
            svc._provider("1:s1")
        except FilesServiceError as exc:
            assert "remote target is unavailable" in str(exc)
        else:
            raise AssertionError("expected FilesServiceError")

    def test_no_fallback_on_remote(self, tmp_path):
        """When REMOTE, RemoteFilesProvider is returned — no fallback to local."""
        app = _make_app(
            tmp_path, rc_enabled=True,
            host_alias="srv", remote_root="/opt/proj",
        )
        svc = FilesService(app=app)
        provider = svc._provider("1:s1")
        assert isinstance(provider, RemoteFilesProvider)
        # Verify it's not LocalFilesProvider
        assert not isinstance(provider, LocalFilesProvider)

    def test_two_sessions_different_providers(self, tmp_path):
        """Two sessions with different RC state get different providers."""
        workdir = str(tmp_path)
        os.makedirs(workdir, exist_ok=True)

        s_local = SimpleNamespace(
            id="s1", workdir=workdir,
            modes=ModeState(remote_control_enabled=False),
        )
        s_remote = SimpleNamespace(
            id="s2", workdir=workdir,
            modes=ModeState(
                ssh_remote_enabled=True,
                remote_control_enabled=True,
                remote_control_host_alias="prod",
            ),
        )

        from app.services.ssh_config_loader import save_ssh_config
        save_ssh_config(workdir, {
            "prod": SSHHostConfig(host="1.1.1.1", user="u", remote_project_root="/srv"),
        })

        def get_by_uid(uid):
            if uid == "1:s1":
                return s_local
            if uid == "1:s2":
                return s_remote
            return None

        miniapp_cfg = SimpleNamespace(max_edit_file_size_kb=5120, enable_delete=True)
        config = SimpleNamespace(miniapp=miniapp_cfg, path=str(tmp_path / "config.yaml"))
        (tmp_path / "config.yaml").write_text("tools: {}")

        app = SimpleNamespace(
            config=config,
            manager=SimpleNamespace(get_by_uid=get_by_uid),
            security=SimpleNamespace(
                resolve_path=lambda root, rel, **kw: SimpleNamespace(resolved_path=os.path.join(root, rel)),
            ),
            remote_control_service=RemoteControlService(),
            ssh_service=SimpleNamespace(),
        )

        svc = FilesService(app=app)
        assert isinstance(svc._provider("1:s1"), LocalFilesProvider)
        assert isinstance(svc._provider("1:s2"), RemoteFilesProvider)
