from __future__ import annotations

from types import SimpleNamespace

import yaml

from app.security import SecurityFacade
from config import load_config


class _Clock:
    def __init__(self, current: float) -> None:
        self.current = float(current)

    def __call__(self) -> float:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += float(seconds)


def _rate_limit_config() -> dict:
    return {
        "enabled": True,
        "backend": "sqlite",
        "default": {
            "limit": 5,
            "window_sec": 60,
            "burst_limit": 2,
            "burst_window_sec": 10,
        },
        "policies": {
            "miniapp.auth": {
                "limit": 2,
                "window_sec": 60,
                "burst_limit": 2,
                "burst_window_sec": 10,
            }
        },
    }


def test_rate_limit_uses_sqlite_storage_and_persists_between_facades(tmp_path) -> None:
    state_path = str(tmp_path / "state-a.json")
    other_state_path = str(tmp_path / "state-b.json")
    clock = _Clock(100.0)
    config = _rate_limit_config()

    first_facade = SecurityFacade.from_config(
        rate_limit_config=config,
        default_rate_limit_state_path=state_path,
        rate_limit_clock=clock,
    )
    assert first_facade.consume_rate_limit("miniapp.auth", "user:1").allowed is True
    assert first_facade.consume_rate_limit("miniapp.auth", "user:1").allowed is True
    clock.advance(10.1)

    persisted_facade = SecurityFacade.from_config(
        rate_limit_config=config,
        default_rate_limit_state_path=state_path,
        rate_limit_clock=clock,
    )
    blocked = persisted_facade.consume_rate_limit("miniapp.auth", "user:1")
    isolated = SecurityFacade.from_config(
        rate_limit_config=config,
        default_rate_limit_state_path=other_state_path,
        rate_limit_clock=clock,
    ).consume_rate_limit("miniapp.auth", "user:1")

    assert blocked.allowed is False
    assert blocked.reason == "window_limit_exceeded"
    assert blocked.retry_after_sec > 0
    assert isolated.allowed is True


def test_rate_limit_supports_burst_and_sliding_window_boundaries(tmp_path) -> None:
    clock = _Clock(1000.0)
    facade = SecurityFacade.from_config(
        rate_limit_config=_rate_limit_config(),
        default_rate_limit_state_path=str(tmp_path / "state.json"),
        rate_limit_clock=clock,
    )

    first = facade.consume_rate_limit("default.scope", "user:9")
    second = facade.consume_rate_limit("default.scope", "user:9")
    burst_blocked = facade.consume_rate_limit("default.scope", "user:9")

    assert first.allowed is True
    assert second.allowed is True
    assert burst_blocked.allowed is False
    assert burst_blocked.reason == "burst_limit_exceeded"

    clock.advance(10.1)
    after_burst = facade.consume_rate_limit("default.scope", "user:9")
    clock.advance(10.1)
    fourth = facade.consume_rate_limit("default.scope", "user:9")
    fifth = facade.consume_rate_limit("default.scope", "user:9")
    clock.advance(10.1)
    window_blocked = facade.consume_rate_limit("default.scope", "user:9")

    assert after_burst.allowed is True
    assert fourth.allowed is True
    assert fifth.allowed is True
    assert window_blocked.allowed is False
    assert window_blocked.reason == "window_limit_exceeded"

    clock.advance(50.0)
    recovered = facade.consume_rate_limit("default.scope", "user:9")
    assert recovered.allowed is True


def test_rate_limit_policies_are_loaded_from_app_config_without_explicit_limits(tmp_path) -> None:
    payload = {
        "telegram": {
            "token": "token",
            "whitelist_chat_ids": [1],
            "admlist_chat_ids": [1],
        },
        "tools": {
            "dummy": {
                "mode": "headless",
                "cmd": ["bash", "-lc", "cat"],
            }
        },
        "defaults": {
            "workdir": str(tmp_path),
            "state_path": str(tmp_path / "state.json"),

            "toolhelp_path": str(tmp_path / "toolhelp.json"),
            "log_path": str(tmp_path / "bot.log"),
        },
        "security": {
            "rate_limits": {
                "enabled": True,
                "backend": "sqlite",
                "policies": {
                    "miniapp.auth": {
                        "limit": 2,
                        "window_sec": 60,
                        "burst_limit": 2,
                        "burst_window_sec": 10,
                    }
                },
            }
        },
    }
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=False), encoding="utf-8")
    cfg = load_config(str(config_path))
    clock = _Clock(200.0)
    bot_app = SimpleNamespace(
        config=cfg,
        system_event_bus=None,
        is_admin=lambda chat_id: int(chat_id) == 1,
        is_user=lambda chat_id: int(chat_id) in {1, 2},
    )

    facade = SecurityFacade.from_app_config(
        bot_app.config,
        is_admin_fn=bot_app.is_admin,
        is_user_fn=bot_app.is_user,
        system_event_bus=bot_app.system_event_bus,
        rate_limit_clock=clock,
    )
    first = facade.consume_rate_limit("miniapp.auth", "user:2")
    second = facade.consume_rate_limit("miniapp.auth", "user:2")
    blocked = facade.consume_rate_limit("miniapp.auth", "user:2")

    assert first.allowed is True
    assert second.allowed is True
    assert blocked.allowed is False
    assert blocked.limit == 2
    assert blocked.reason == "burst_limit_exceeded"
