"""Core i18n tests: resolver, translator, plural, language_names, persist flow."""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from i18n.resolver import (
    map_telegram_language_code,
    resolve_language,
)
from i18n.translator import _cache, get_catalog, reload_catalogs, t
from i18n.plural import plural
from utils.lang import resolve_user_lang


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(
    user_languages: dict[int, str] | None = None,
    default_language: str | None = "ru",
) -> Any:
    """Build a minimal AppConfig-like stub."""
    tg = MagicMock()
    tg.user_languages = user_languages or {}
    defaults = MagicMock()
    defaults.default_language = default_language
    cfg = MagicMock()
    cfg.telegram = tg
    cfg.defaults = defaults
    return cfg


def _flatten_keys(d: dict, prefix: str = "") -> set[str]:
    """Flatten a nested dict into dot-notation key set."""
    keys: set[str] = set()
    for k, v in d.items():
        full = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            keys |= _flatten_keys(v, full)
        else:
            keys.add(full)
    return keys


# ---------------------------------------------------------------------------
# map_telegram_language_code
# ---------------------------------------------------------------------------

def test_map_telegram_language_code_known_codes() -> None:
    assert map_telegram_language_code("en-US") == "en"
    assert map_telegram_language_code("zh-hans") == "zh"
    assert map_telegram_language_code("zh-CN") == "zh"
    assert map_telegram_language_code("zh-TW") == "zh"
    assert map_telegram_language_code("de-AT") == "de"
    assert map_telegram_language_code("ru") == "ru"
    assert map_telegram_language_code("de") == "de"


def test_map_telegram_language_code_unsupported_returns_none() -> None:
    assert map_telegram_language_code("fr") is None
    assert map_telegram_language_code("es") is None
    assert map_telegram_language_code("ja") is None
    assert map_telegram_language_code("") is None
    assert map_telegram_language_code(None) is None


def test_map_telegram_language_code_case_insensitive() -> None:
    assert map_telegram_language_code("EN-US") == "en"
    assert map_telegram_language_code("ZH-HANS") == "zh"


# ---------------------------------------------------------------------------
# resolve_language
# ---------------------------------------------------------------------------

def test_resolve_language_explicit_wins() -> None:
    cfg = _make_config(user_languages={1: "en"}, default_language="ru")
    assert resolve_language(1, "de", cfg) == "en"


def test_resolve_language_auto_detect_over_default() -> None:
    cfg = _make_config(user_languages={}, default_language="ru")
    assert resolve_language(1, "de", cfg) == "de"


def test_resolve_language_default_when_no_auto() -> None:
    cfg = _make_config(user_languages={}, default_language="en")
    assert resolve_language(1, "fr", cfg) == "en"


def test_resolve_language_hard_fallback() -> None:
    cfg = _make_config(user_languages={}, default_language=None)
    assert resolve_language(1, None, cfg) == "ru"


def test_resolve_language_user_id_none() -> None:
    cfg = _make_config(user_languages={}, default_language="ru")
    assert resolve_language(None, "en", cfg) == "en"


def test_resolve_language_unsupported_saved_value_ignored() -> None:
    cfg = _make_config(user_languages={1: "fr"}, default_language="ru")
    # "fr" is unsupported, skip to step 2 (auto-detect)
    assert resolve_language(1, "de", cfg) == "de"


# ---------------------------------------------------------------------------
# t() — translator
# ---------------------------------------------------------------------------

def test_t_returns_translation() -> None:
    reload_catalogs()
    result = t("common.ok", "en")
    assert result == "OK"


def test_t_fallback_to_ru() -> None:
    reload_catalogs()
    # Inject a key that exists only in ru catalog
    with pytest.MonkeyPatch().context() as mp:
        ru_catalog = dict(get_catalog("ru"))
        ru_catalog.setdefault("_test_only", {})["ru_only_key"] = "только ру"
        en_catalog = dict(get_catalog("en"))
        # en_catalog does NOT have _test_only
        mp.setitem(_cache, "ru", ru_catalog)
        mp.setitem(_cache, "en", en_catalog)
        result = t("_test_only.ru_only_key", "en")
        assert result == "только ру"


def test_t_returns_key_when_missing_everywhere() -> None:
    reload_catalogs()
    missing = "nonexistent.deeply.nested.key"
    assert t(missing, "en") == missing


def test_t_warning_on_missing_key(caplog) -> None:
    reload_catalogs()
    missing = "nonexistent.key.for.warning"
    with caplog.at_level("WARNING", logger="i18n.translator"):
        t(missing, "en")
    assert any("missing" in r.message for r in caplog.records)


def test_t_parameter_substitution() -> None:
    reload_catalogs()
    result = t("msg.lang.changed", "en", lang="English")
    assert "English" in result


def test_t_missing_param_no_exception() -> None:
    reload_catalogs()
    # Should not raise even though {lang} is not provided
    result = t("msg.lang.changed", "en")
    assert isinstance(result, str)


def test_t_unsupported_lang_uses_ru() -> None:
    reload_catalogs()
    assert t("common.ok", "fr") == t("common.ok", "ru")


def test_reload_catalogs_clears_cache() -> None:
    # Prime the cache
    _ = get_catalog("en")
    with pytest.MonkeyPatch().context() as mp:
        mp.setitem(_cache, "en", {"injected": "value"})
        assert _cache["en"] == {"injected": "value"}
    reload_catalogs()
    fresh = get_catalog("en")
    assert "injected" not in fresh


# ---------------------------------------------------------------------------
# plural()
# ---------------------------------------------------------------------------

def test_plural_ru_forms() -> None:
    forms = ["{n} задача", "{n} задачи", "{n} задач"]
    assert plural(1, "ru", forms) == "1 задача"
    assert plural(2, "ru", forms) == "2 задачи"
    assert plural(5, "ru", forms) == "5 задач"
    assert plural(11, "ru", forms) == "11 задач"   # trap: 11%10==1 but 11%100==11
    assert plural(21, "ru", forms) == "21 задача"


def test_plural_en_forms() -> None:
    forms = ["item", "items"]
    assert plural(1, "en", forms) == "item"
    assert plural(2, "en", forms) == "items"


def test_plural_de_forms() -> None:
    forms = ["Element", "Elemente"]
    assert plural(1, "de", forms) == "Element"
    assert plural(3, "de", forms) == "Elemente"


def test_plural_zh_single_form() -> None:
    forms = ["项目"]
    assert plural(100, "zh", forms) == "项目"


def test_plural_out_of_bounds_returns_str_n() -> None:
    # en needs index 1 for plural, but only 1 form provided
    forms = ["item"]
    assert plural(5, "en", forms) == "5"


def test_plural_empty_forms_returns_str_n() -> None:
    assert plural(3, "ru", []) == "3"


# ---------------------------------------------------------------------------
# Catalog parity
# ---------------------------------------------------------------------------

def test_catalog_parity_all_keys_present_in_all_langs() -> None:
    reload_catalogs()
    ru_keys = _flatten_keys(get_catalog("ru"))
    for lang in ("en", "zh", "de"):
        lang_keys = _flatten_keys(get_catalog(lang))
        assert lang_keys == ru_keys, (
            f"Catalog key mismatch for lang={lang!r}. "
            f"Missing: {ru_keys - lang_keys}. Extra: {lang_keys - ru_keys}"
        )


# ---------------------------------------------------------------------------
# maybe_persist_user_language
# ---------------------------------------------------------------------------

def test_persist_flow_writes_on_first_contact() -> None:
    from app.services.i18n_service import maybe_persist_user_language
    from app.services.config_service import ConfigDraftSaveResult

    cfg = _make_config(user_languages={}, default_language="ru")
    mock_svc = MagicMock()
    ok_result = ConfigDraftSaveResult(
        ok=True, revision="rev1", diff="", changed=True,
        restart_required=[], reloadable=[], errors=[], backup_path=None,
    )
    mock_svc.set_user_language = AsyncMock(return_value=ok_result)

    asyncio.run(maybe_persist_user_language(1, "en", cfg, mock_svc))

    mock_svc.set_user_language.assert_called_once_with(1, "en")
    # W2: live in-memory config must reflect the persisted choice immediately,
    # so resolve_user_lang() sees it without waiting for a full reload.
    assert cfg.telegram.user_languages == {1: "en"}


def test_persist_flow_idempotent() -> None:
    from app.services.i18n_service import maybe_persist_user_language

    cfg = _make_config(user_languages={1: "en"}, default_language="ru")
    mock_svc = MagicMock()
    mock_svc.set_user_language = AsyncMock()

    asyncio.run(maybe_persist_user_language(1, "en", cfg, mock_svc))

    mock_svc.set_user_language.assert_not_called()


def test_persist_flow_skips_unknown_code() -> None:
    from app.services.i18n_service import maybe_persist_user_language

    cfg = _make_config(user_languages={}, default_language="ru")
    mock_svc = MagicMock()
    mock_svc.set_user_language = AsyncMock()

    asyncio.run(maybe_persist_user_language(1, "fr", cfg, mock_svc))

    mock_svc.set_user_language.assert_not_called()


def test_persist_flow_handles_failure_gracefully() -> None:
    from app.services.i18n_service import maybe_persist_user_language
    from app.services.config_service import ConfigDraftSaveResult

    cfg = _make_config(user_languages={}, default_language="ru")
    mock_svc = MagicMock()

    failure = ConfigDraftSaveResult(
        ok=False, revision="r", diff="", changed=False,
        restart_required=[], reloadable=[],
        errors=["max retries exceeded on revision mismatch"], backup_path=None,
    )
    mock_svc.set_user_language = AsyncMock(return_value=failure)

    # Single call (retry lives inside set_user_language); must not raise.
    asyncio.run(maybe_persist_user_language(1, "en", cfg, mock_svc))

    mock_svc.set_user_language.assert_called_once_with(1, "en")
    # On failure the in-memory config must NOT be mutated.
    assert cfg.telegram.user_languages == {}


def _make_config_service_stub(side_effect):
    """Bind the real ConfigService.set_user_language onto a stubbed service."""
    from app.services.config_service import ConfigDraftSaveResult  # noqa: F401

    svc = MagicMock()
    cfg = MagicMock()
    cfg.telegram.user_languages = {}
    svc.load = AsyncMock(return_value=cfg)
    svc.current_revision = AsyncMock(return_value="rev")
    svc._as_dict = MagicMock(return_value={"telegram": {}})
    svc.save_draft_with_revision = AsyncMock(side_effect=side_effect)
    return svc


def test_set_user_language_retries_on_revision_mismatch() -> None:
    from app.services.config_service import ConfigService, ConfigDraftSaveResult

    mismatch = ConfigDraftSaveResult(
        ok=False, revision="r", diff="", changed=False,
        restart_required=[], reloadable=[], errors=["revision mismatch"], backup_path=None,
    )
    ok_result = ConfigDraftSaveResult(
        ok=True, revision="r2", diff="", changed=True,
        restart_required=[], reloadable=["telegram.user_languages"], errors=[],
        backup_path=None,
    )
    svc = _make_config_service_stub([mismatch, mismatch, ok_result])

    result = asyncio.run(ConfigService.set_user_language(svc, 1, "en"))

    assert result.ok
    assert svc.save_draft_with_revision.call_count == 3


def test_set_user_language_gives_up_after_max_retries() -> None:
    from app.services.config_service import ConfigService, ConfigDraftSaveResult

    mismatch = ConfigDraftSaveResult(
        ok=False, revision="r", diff="", changed=False,
        restart_required=[], reloadable=[], errors=["revision mismatch"], backup_path=None,
    )
    svc = _make_config_service_stub([mismatch, mismatch, mismatch])

    result = asyncio.run(ConfigService.set_user_language(svc, 1, "en"))

    assert not result.ok
    assert any("revision mismatch" in e for e in result.errors)
    assert svc.save_draft_with_revision.call_count == 3


# ---------------------------------------------------------------------------
# resolve_user_lang
# ---------------------------------------------------------------------------

def test_resolve_user_lang_by_user_id() -> None:
    cfg = _make_config(user_languages={42: "de"})
    assert resolve_user_lang(cfg, user_id=42) == "de"


def test_resolve_user_lang_by_chat_id_fallback() -> None:
    cfg = _make_config(user_languages={100: "zh"})
    assert resolve_user_lang(cfg, chat_id=100) == "zh"


def test_resolve_user_lang_default_language() -> None:
    cfg = _make_config(user_languages={}, default_language="en")
    assert resolve_user_lang(cfg, user_id=1, chat_id=1) == "en"


def test_resolve_user_lang_hard_fallback() -> None:
    cfg = _make_config(user_languages={}, default_language=None)
    assert resolve_user_lang(cfg, user_id=1) == "ru"
