from __future__ import annotations

import asyncio

from desktop.main import bootstrap_facade

from tests.smoke._smoke_support import build_config, write_config


def test_desktop_entrypoint_smoke_bootstraps_and_starts_facade(tmp_path) -> None:
    cfg = build_config(tmp_path, intent="desktop_entrypoint")
    cfg_path = write_config(cfg, tmp_path / "config.yaml")

    async def _run() -> None:
        facade, ui_state_service = await bootstrap_facade(config_path=str(cfg_path))
        await facade.start(validate_secrets=False)
        await ui_state_service.wait_ready()
        assert facade.started is True
        assert facade.runtime_params is not None
        await facade.shutdown()

    asyncio.run(_run())
