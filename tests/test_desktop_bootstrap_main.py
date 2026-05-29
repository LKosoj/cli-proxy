import asyncio
import os

import yaml

from desktop.main import bootstrap_facade


def test_desktop_bootstrap_builds_facade_in_expected_order(tmp_path) -> None:
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "telegram": {"token": "t", "whitelist_chat_ids": [1], "admlist_chat_ids": [1]},
                "tools": {
                    "dummy": {"mode": "headless", "cmd": ["bash", "-lc", "cat"], "enabled": True},
                },
                "defaults": {
                    "workdir": str(tmp_path / "workdir"),
                    "state_path": str(tmp_path / "runtime" / "state.json"),
                    "toolhelp_path": str(tmp_path / "runtime" / "toolhelp.json"),
                    "log_path": str(tmp_path / "logs" / "bot.log"),
                },
                "mcp": {"enabled": False},
                "mcp_clients": [],
                "presets": [],
                "miniapp": {"enabled": False},
            },
            sort_keys=False,
            allow_unicode=False,
        ),
        encoding="utf-8",
    )

    async def _run():
        facade, ui_state_service = await bootstrap_facade(config_path=str(cfg_path))
        # Registry must be initialized before facade is used by UI.
        assert facade.mode_registry_service is not None
        assert facade.mode_registry_service.registry is not None

        # Services must be injected before MainWindow creation (facade already has them).
        assert facade.config_service is not None
        assert facade.session_service is not None
        assert facade.task_service is not None
        assert facade.git_service is not None
        assert facade.ui_state_service is ui_state_service

        await facade.start()
        await ui_state_service.wait_ready()
        assert facade.runtime_params is not None
        assert os.path.isdir(facade.runtime_params.workdir)

    asyncio.run(_run())
