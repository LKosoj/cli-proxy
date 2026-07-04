from bot import BotApp
from config import AppConfig, DefaultsConfig, MCPConfig, MiniAppConfig, TelegramConfig, ToolConfig, load_config


def _build_app(public_url: str, base_path: str = "/cli-proxy") -> BotApp:
    cfg = AppConfig(
        telegram=TelegramConfig(token="t", whitelist_chat_ids=[1], admlist_chat_ids=[1]),
        tools={"dummy": ToolConfig(name="dummy", mode="headless", cmd=["bash", "-lc", "cat"])},
        defaults=DefaultsConfig(workdir=".", state_path="state.json", toolhelp_path="toolhelp.json", log_path="bot.log"),
        mcp=MCPConfig(enabled=False),
        mcp_clients=[],
        presets=[],
        path="config.yaml",
        miniapp=MiniAppConfig(enabled=True, base_path=base_path, public_url=public_url),
    )
    app = BotApp(cfg)
    app.runtime_service._miniapp_daily_cache_buster = lambda: "20260704"
    return app


def test_build_miniapp_webapp_url_appends_base_path_when_public_url_has_no_path() -> None:
    app = _build_app("https://example.com")
    assert app._build_miniapp_webapp_url() == "https://example.com/cli-proxy/?v=20260704"


def test_build_miniapp_webapp_url_adds_trailing_slash_when_public_url_has_path_without_slash() -> None:
    app = _build_app("https://example.com/cli-proxy")
    assert app._build_miniapp_webapp_url() == "https://example.com/cli-proxy/?v=20260704"


def test_build_miniapp_webapp_url_preserves_existing_query_and_adds_cache_buster() -> None:
    app = _build_app("https://example.com/cli-proxy?source=telegram")
    assert app._build_miniapp_webapp_url() == "https://example.com/cli-proxy/?source=telegram&v=20260704"


def test_build_miniapp_webapp_url_replaces_existing_cache_buster() -> None:
    app = _build_app("https://example.com/cli-proxy?v=old&source=telegram")
    assert app._build_miniapp_webapp_url() == "https://example.com/cli-proxy/?source=telegram&v=20260704"


def test_build_miniapp_webapp_url_rejects_relative_public_url() -> None:
    app = _build_app("/cli-proxy")
    assert app._build_miniapp_webapp_url() is None


def test_load_config_reads_miniapp_public_url(tmp_path) -> None:
    p = tmp_path / "config.yaml"
    p.write_text(
        """
telegram:
  token: "t"
  whitelist_chat_ids: [1]
  admlist_chat_ids: [1]
tools:
  dummy:
    mode: headless
    cmd: ["bash", "-lc", "cat"]
defaults:
  workdir: "."
miniapp:
  enabled: true
  base_path: "/cli-proxy"
  public_url: "https://example.com/cli-proxy"
""".strip(),
        encoding="utf-8",
    )
    cfg = load_config(str(p))
    assert cfg.miniapp.public_url == "https://example.com/cli-proxy"
