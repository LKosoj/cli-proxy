import asyncio
import os

import yaml

from app.services.config_service import ConfigService, FileConfigProvider


def test_config_service_save_atomic_keeps_secrets_plaintext(tmp_path) -> None:
    """
    Регрессия: ConfigService.save_atomic не должен редактировать/подменять секреты плейсхолдерами.
    Секреты обязаны сохраняться в YAML как обычный текст (без ${...} и без __keyring__:*).
    """
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "telegram": {
                    "token": "old",
                    "whitelist_chat_ids": [1],
                    "admlist_chat_ids": [1],
                },
                "tools": {},
                "defaults": {
                    "workdir": str(tmp_path),
                    "idle_timeout_sec": 10,
                    "state_path": str(tmp_path / "state.json"),
                    "toolhelp_path": str(tmp_path / "toolhelp.txt"),
                    "log_path": str(tmp_path / "bot.log"),
                    "openai_api_key": "old-openai",
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

    provider = FileConfigProvider(str(cfg_path))
    service = ConfigService(provider)
    cfg = asyncio.run(service.load())

    cfg.telegram.token = "raw-telegram"
    cfg.defaults.openai_api_key = "raw-openai"

    asyncio.run(service.save_atomic(cfg))

    content = cfg_path.read_text(encoding="utf-8")
    assert "raw-telegram" in content
    assert "raw-openai" in content
    assert "__keyring__:" not in content
    assert "${" not in content

    loaded = yaml.safe_load(content)
    assert loaded["telegram"]["token"] == "raw-telegram"
    assert loaded["defaults"]["openai_api_key"] == "raw-openai"
    assert os.path.exists(str(cfg_path) + ".bak")


def test_config_service_save_draft_with_revision_keeps_new_secrets_plaintext(tmp_path) -> None:
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "telegram": {
                    "token": "old",
                    "whitelist_chat_ids": [1],
                    "admlist_chat_ids": [1],
                },
                "tools": {},
                "defaults": {
                    "workdir": str(tmp_path),
                    "idle_timeout_sec": 10,
                    "state_path": str(tmp_path / "state.json"),
                    "toolhelp_path": str(tmp_path / "toolhelp.txt"),
                    "log_path": str(tmp_path / "bot.log"),
                    "openai_api_key": "old-openai",
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
    service = ConfigService(FileConfigProvider(str(cfg_path)))
    draft = service._as_dict(asyncio.run(service.load()))
    revision = asyncio.run(service.current_revision())
    draft["telegram"]["token"] = "new-telegram"
    draft["defaults"]["openai_api_key"] = "new-openai"

    result = asyncio.run(service.save_draft_with_revision(draft, expected_revision=revision))

    assert result.ok is True
    assert result.secret_changed == ["defaults.openai_api_key", "telegram.token"]
    content = cfg_path.read_text(encoding="utf-8")
    assert "new-telegram" in content
    assert "new-openai" in content
    assert "__CLI_PROXY_SECRET_UNCHANGED__" not in content
    loaded = yaml.safe_load(content)
    assert loaded["telegram"]["token"] == "new-telegram"
    assert loaded["defaults"]["openai_api_key"] == "new-openai"
