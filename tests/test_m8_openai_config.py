"""Tests for resolve_openai_config (M8: deduplicate OpenAI config building)."""
from __future__ import annotations

import types
from typing import Optional

from modes.sdk.runtime.openai_client import resolve_openai_config


def _fake_config(
    api_key: Optional[str] = None,
    model: Optional[str] = None,
    big_model: Optional[str] = None,
    base_url: Optional[str] = None,
) -> object:
    defaults = types.SimpleNamespace(
        openai_api_key=api_key,
        openai_model=model,
        openai_big_model=big_model,
        openai_base_url=base_url,
    )
    return types.SimpleNamespace(defaults=defaults)


# ---------------------------------------------------------------------------
# env_priority=True: env overrides config (agent_core behaviour)
# ---------------------------------------------------------------------------

class TestEnvPriorityTrue:
    def test_env_overrides_config_api_key(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "env-key")
        monkeypatch.setenv("OPENAI_MODEL", "env-model")
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)

        cfg = _fake_config(api_key="cfg-key", model="cfg-model")
        result = resolve_openai_config(cfg, model_key="openai_model", env_priority=True)

        assert result is not None
        api_key, model, base_url = result
        assert api_key == "env-key"
        assert model == "env-model"
        assert base_url == "https://api.openai.com"

    def test_env_overrides_config_base_url(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "env-key")
        monkeypatch.setenv("OPENAI_MODEL", "env-model")
        monkeypatch.setenv("OPENAI_BASE_URL", "https://env.example.com/")

        cfg = _fake_config(api_key="cfg-key", model="cfg-model", base_url="https://cfg.example.com")
        result = resolve_openai_config(cfg, model_key="openai_model", env_priority=True)

        assert result is not None
        _, _, base_url = result
        assert base_url == "https://env.example.com"  # trailing slash stripped

    def test_config_used_as_fallback_when_env_missing(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_MODEL", raising=False)
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)

        cfg = _fake_config(api_key="cfg-key", model="cfg-model", base_url="https://cfg.example.com")
        result = resolve_openai_config(cfg, model_key="openai_model", env_priority=True)

        assert result is not None
        api_key, model, base_url = result
        assert api_key == "cfg-key"
        assert model == "cfg-model"
        assert base_url == "https://cfg.example.com"


# ---------------------------------------------------------------------------
# env_priority=False: config overrides env (summary behaviour)
# ---------------------------------------------------------------------------

class TestEnvPriorityFalse:
    def test_config_overrides_env(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "env-key")
        monkeypatch.setenv("OPENAI_BIG_MODEL", "env-big-model")
        monkeypatch.setenv("OPENAI_BASE_URL", "https://env.example.com")

        cfg = _fake_config(api_key="cfg-key", big_model="cfg-big-model", base_url="https://cfg.example.com")
        result = resolve_openai_config(cfg, model_key="openai_big_model", env_priority=False)

        assert result is not None
        api_key, model, base_url = result
        assert api_key == "cfg-key"
        assert model == "cfg-big-model"
        assert base_url == "https://cfg.example.com"

    def test_env_used_as_fallback_when_config_missing(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "env-key")
        monkeypatch.setenv("OPENAI_BIG_MODEL", "env-big-model")
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)

        cfg = _fake_config()  # all None
        result = resolve_openai_config(cfg, model_key="openai_big_model", env_priority=False)

        assert result is not None
        api_key, model, _ = result
        assert api_key == "env-key"
        assert model == "env-big-model"


# ---------------------------------------------------------------------------
# model_key="openai_big_model" reads the big model field and OPENAI_BIG_MODEL env
# ---------------------------------------------------------------------------

class TestModelKeyBigModel:
    def test_reads_big_model_from_config(self, monkeypatch):
        monkeypatch.delenv("OPENAI_BIG_MODEL", raising=False)
        monkeypatch.delenv("OPENAI_MODEL", raising=False)
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)

        cfg = _fake_config(api_key="k", model="small", big_model="big-model-xyz")
        result = resolve_openai_config(cfg, model_key="openai_big_model", env_priority=False)

        assert result is not None
        _, model, _ = result
        assert model == "big-model-xyz"

    def test_reads_big_model_from_env_var(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "env-key")
        monkeypatch.setenv("OPENAI_BIG_MODEL", "env-big-xyz")
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)

        result = resolve_openai_config(None, model_key="openai_big_model", env_priority=True)

        assert result is not None
        _, model, _ = result
        assert model == "env-big-xyz"

    def test_big_model_env_not_leaked_into_regular_model_key(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        monkeypatch.setenv("OPENAI_BIG_MODEL", "env-big")
        monkeypatch.delenv("OPENAI_MODEL", raising=False)
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)

        # model_key="openai_model" should NOT read OPENAI_BIG_MODEL
        result = resolve_openai_config(None, model_key="openai_model", env_priority=True)
        assert result is None  # OPENAI_MODEL not set → None


# ---------------------------------------------------------------------------
# Returns None when api_key or model is missing
# ---------------------------------------------------------------------------

class TestReturnNone:
    def test_none_when_no_api_key(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_MODEL", raising=False)
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)

        cfg = _fake_config(model="some-model")
        assert resolve_openai_config(cfg, model_key="openai_model", env_priority=True) is None

    def test_none_when_no_model(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_MODEL", raising=False)
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)

        cfg = _fake_config(api_key="k")
        assert resolve_openai_config(cfg, model_key="openai_model", env_priority=True) is None

    def test_none_when_no_config_and_no_env(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_MODEL", raising=False)
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)

        assert resolve_openai_config(None, model_key="openai_model", env_priority=True) is None

    def test_none_when_config_is_none_and_big_model_env_missing(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        monkeypatch.delenv("OPENAI_BIG_MODEL", raising=False)
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)

        assert resolve_openai_config(None, model_key="openai_big_model", env_priority=True) is None


# ---------------------------------------------------------------------------
# Default base_url
# ---------------------------------------------------------------------------

class TestDefaultBaseUrl:
    def test_default_base_url_when_not_set(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        monkeypatch.setenv("OPENAI_MODEL", "m")
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)

        result = resolve_openai_config(None, model_key="openai_model", env_priority=True)

        assert result is not None
        _, _, base_url = result
        assert base_url == "https://api.openai.com"

    def test_trailing_slash_stripped_from_base_url(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        monkeypatch.setenv("OPENAI_MODEL", "m")
        monkeypatch.setenv("OPENAI_BASE_URL", "https://custom.example.com/v1/")

        result = resolve_openai_config(None, model_key="openai_model", env_priority=True)

        assert result is not None
        _, _, base_url = result
        assert base_url == "https://custom.example.com/v1"
