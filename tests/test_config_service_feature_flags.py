import asyncio

import yaml

from app.services.config_service import ConfigService, FileConfigProvider


def test_config_service_is_feature_enabled_uses_defaults_flags(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "telegram": {"token": "t", "whitelist_chat_ids": [1]},
                "tools": {},
                "defaults": {
                    "workdir": str(tmp_path),
                    "clarification_enabled": True,
                    "pending_input_confirmation_enabled": False,
                    "assistant_preview_enabled": False,
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

    svc = ConfigService(FileConfigProvider(str(cfg_path)))

    assert asyncio.run(svc.is_feature_enabled("clarification_enabled")) is True
    assert asyncio.run(svc.is_feature_enabled("pending_input_confirmation_enabled")) is False
    assert asyncio.run(svc.is_feature_enabled("defaults.assistant_preview_enabled")) is False
    assert asyncio.run(svc.is_feature_enabled("unknown_flag")) is False
    assert asyncio.run(svc.is_feature_enabled("")) is False
