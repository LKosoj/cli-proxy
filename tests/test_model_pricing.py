from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.model_pricing import ModelPricing, build_model_pricing

_PRICES = {
    "gpt-5.3-codex": {
        "input_cost_per_token": 1e-6,
        "output_cost_per_token": 1e-5,
        "cache_read_input_token_cost": 1e-7,
    },
    "openrouter/qwen/qwen3-coder": {
        "input_cost_per_token": 2e-7,
        "output_cost_per_token": 8e-7,
    },
    "claude-opus-4-5": {
        "input_cost_per_token": 5e-6,
        "output_cost_per_token": 25e-6,
        "cache_creation_input_token_cost": 6.25e-6,
        "cache_read_input_token_cost": 5e-7,
    },
}


def _pricing(tmp_path: Path, *, prices: dict | None = None) -> ModelPricing:
    cache_path = tmp_path / "model_prices.json"
    cache_path.write_text(json.dumps(prices if prices is not None else _PRICES), encoding="utf-8")
    service = ModelPricing(cache_path=cache_path)
    return service


def test_estimate_usd_sums_rates_by_token_kind(tmp_path: Path) -> None:
    pricing = _pricing(tmp_path)

    amount = pricing.estimate_usd(
        "gpt-5.3-codex",
        {"input": 1_000_000, "output": 100_000, "cache_read": 2_000_000},
    )

    assert amount == pytest.approx(1.0 + 1.0 + 0.2)


def test_estimate_usd_falls_back_to_input_rate_for_cache(tmp_path: Path) -> None:
    pricing = _pricing(tmp_path)

    amount = pricing.estimate_usd("openrouter/qwen/qwen3-coder", {"cache_write": 1_000_000})

    assert amount == pytest.approx(0.2)


def test_estimate_usd_matches_model_by_provider_suffix(tmp_path: Path) -> None:
    pricing = _pricing(tmp_path)

    amount = pricing.estimate_usd("qwen/qwen3-coder", {"input": 1_000_000})

    assert amount == pytest.approx(0.2)


def test_estimate_usd_strips_date_suffix(tmp_path: Path) -> None:
    pricing = _pricing(tmp_path)

    amount = pricing.estimate_usd(
        "claude-opus-4-5-20260101",
        {"output": 1_000_000, "cache_write": 1_000_000},
    )

    assert amount == pytest.approx(25.0 + 6.25)


def test_estimate_usd_returns_none_for_unknown_model(tmp_path: Path) -> None:
    pricing = _pricing(tmp_path)

    assert pricing.estimate_usd("totally-unknown-model", {"input": 1_000}) is None


def test_estimate_usd_returns_none_without_priced_tokens(tmp_path: Path) -> None:
    pricing = _pricing(tmp_path)

    assert pricing.estimate_usd("gpt-5.3-codex", {"input": 0, "output": 0}) is None


def test_load_prices_does_not_hit_network_when_cache_is_fresh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pricing = _pricing(tmp_path)

    def _unexpected_fetch() -> None:
        raise AssertionError("fresh cache must not trigger a network fetch")

    monkeypatch.setattr(pricing, "_fetch_prices", _unexpected_fetch)

    assert pricing.estimate_usd("gpt-5.3-codex", {"input": 1_000_000}) == pytest.approx(1.0)


def test_load_prices_uses_stale_cache_when_fetch_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache_path = tmp_path / "model_prices.json"
    cache_path.write_text(json.dumps(_PRICES), encoding="utf-8")
    pricing = ModelPricing(cache_path=cache_path)
    monkeypatch.setattr(pricing, "_cache_expired", lambda: True)
    monkeypatch.setattr(pricing, "_fetch_prices", lambda: None)

    assert pricing.estimate_usd("gpt-5.3-codex", {"input": 1_000_000}) == pytest.approx(1.0)


def test_fetch_prices_is_not_retried_immediately_after_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def _failing_urlopen(request: object, timeout: float = 0.0) -> None:
        calls.append(str(timeout))
        raise TimeoutError("network down")

    monkeypatch.setattr("app.services.model_pricing.urllib.request.urlopen", _failing_urlopen)
    pricing = ModelPricing(cache_path=tmp_path / "missing.json")

    assert pricing._fetch_prices() is None
    assert pricing._fetch_prices() is None
    assert len(calls) == 1


def test_disabled_pricing_never_reads_cache(tmp_path: Path) -> None:
    cache_path = tmp_path / "model_prices.json"
    cache_path.write_text(json.dumps(_PRICES), encoding="utf-8")
    pricing = ModelPricing(cache_path=cache_path, enabled=False)

    assert pricing.estimate_usd("gpt-5.3-codex", {"input": 1_000_000}) is None


def test_build_model_pricing_keeps_cache_next_to_state_file(tmp_path: Path) -> None:
    pricing = build_model_pricing(tmp_path / "state.json")

    assert pricing._cache_path == tmp_path / ".cli-proxy" / "runtime" / "model_prices.json"
