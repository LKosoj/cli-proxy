from __future__ import annotations

import bot as bot_module

from tests.smoke._smoke_support import build_config, write_config


def test_bot_entrypoint_smoke_invokes_build_app_and_run_polling(tmp_path, monkeypatch) -> None:
    cfg = build_config(tmp_path, intent="bot_entrypoint")
    cfg_path = write_config(cfg, tmp_path / "config.yaml")
    polling_calls: list[dict[str, float | int]] = []
    dotenv_calls: list[str] = []
    validated_paths: list[str] = []
    loaded_paths: list[str] = []

    class _FakeApplication:
        def run_polling(self, *, poll_interval, timeout) -> None:
            polling_calls.append({"poll_interval": float(poll_interval), "timeout": int(timeout)})

    monkeypatch.setattr(bot_module, "CONFIG_PATH", str(cfg_path))
    monkeypatch.setattr(
        bot_module,
        "load_dotenv_near",
        lambda path, filename=".env", override=False: dotenv_calls.append(
            f"{path}|{filename}|{int(bool(override))}"
        ),
    )
    monkeypatch.setattr(
        bot_module,
        "load_validated_settings",
        lambda path: validated_paths.append(str(path)) or {"path": str(path)},
    )
    monkeypatch.setattr(
        bot_module,
        "load_config",
        lambda path: loaded_paths.append(str(path)) or cfg,
    )
    monkeypatch.setattr(bot_module, "build_app", lambda config: _FakeApplication())

    bot_module.main()

    assert dotenv_calls == [f"{cfg_path}|.env|0"]
    assert validated_paths == [str(cfg_path)]
    assert loaded_paths == [str(cfg_path)]
    assert polling_calls == [
        {
            "poll_interval": float(getattr(cfg.telegram, "poll_interval_sec", 0.0)),
            "timeout": int(getattr(cfg.telegram, "polling_timeout_sec", 5)),
        }
    ]
