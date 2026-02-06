import os

from dotenv_loader import load_dotenv, parse_dotenv


def test_parse_dotenv_basic():
    text = """
    # comment
    WOLFRAM_APP_ID=abc
    export OPENAI_API_KEY="k=v"
    EMPTY=
    SPACED=hello world
    WITHHASH=val#1
    INLINE=val #comment
    QUOTED_HASH="val #comment"
    """
    parsed = parse_dotenv(text)
    assert parsed["WOLFRAM_APP_ID"] == "abc"
    assert parsed["OPENAI_API_KEY"] == "k=v"
    assert parsed["EMPTY"] == ""
    assert parsed["SPACED"] == "hello world"
    assert parsed["WITHHASH"] == "val#1"
    assert parsed["INLINE"] == "val"
    assert parsed["QUOTED_HASH"] == "val #comment"


def test_load_dotenv_does_not_override_existing(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    env_path.write_text("WOLFRAM_APP_ID=from_file\n", encoding="utf-8")
    monkeypatch.setenv("WOLFRAM_APP_ID", "from_env")
    applied = load_dotenv(str(env_path), override=False)
    assert applied == {}
    assert os.environ["WOLFRAM_APP_ID"] == "from_env"


def test_load_dotenv_override(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    env_path.write_text("WOLFRAM_APP_ID=from_file\n", encoding="utf-8")
    monkeypatch.setenv("WOLFRAM_APP_ID", "from_env")
    applied = load_dotenv(str(env_path), override=True)
    assert applied["WOLFRAM_APP_ID"] == "from_file"
    assert os.environ["WOLFRAM_APP_ID"] == "from_file"


def test_load_config_loads_dotenv_near_config(tmp_path, monkeypatch):
    # Ensure config.load_config loads .env from the same directory.
    monkeypatch.delenv("WOLFRAM_APP_ID", raising=False)
    (tmp_path / ".env").write_text("WOLFRAM_APP_ID=near_config\n", encoding="utf-8")
    (tmp_path / "config.yaml").write_text(
        "\n".join(
            [
                "telegram:",
                "  token: ''",
                "  whitelist_chat_ids: []",
                "tools: {}",
                "defaults:",
                f"  workdir: '{tmp_path.as_posix()}'",
                "mcp: { enabled: false }",
                "mcp_clients: []",
                "presets: []",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    from config import load_config

    _ = load_config(str(tmp_path / "config.yaml"))
    assert os.environ["WOLFRAM_APP_ID"] == "near_config"
