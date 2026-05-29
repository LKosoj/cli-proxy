from __future__ import annotations

from pathlib import Path

import yaml

from app.services.lint_evolution import weights_store


def test_bootstrap_creates_active_from_template(tmp_path: Path) -> None:
    workdir = str(tmp_path)
    bundle = weights_store.bootstrap(workdir)
    assert bundle.schema_version == 1
    assert bundle.generated_by == "bootstrap"
    assert "category_security" in bundle.weights
    assert "apply" in bundle.thresholds


def test_bootstrap_if_missing_is_idempotent(tmp_path: Path) -> None:
    workdir = str(tmp_path)
    weights_store.bootstrap_if_missing(workdir)
    first = weights_store.load_active(workdir)
    weights_store.bootstrap_if_missing(workdir)
    second = weights_store.load_active(workdir)
    assert first.weights == second.weights
    assert first.thresholds == second.thresholds


def test_save_active_round_trip(tmp_path: Path) -> None:
    workdir = str(tmp_path)
    bundle = weights_store.bootstrap(workdir)
    bundle.weights["category_security"] = 9.99
    bundle.generated_by = "test"
    weights_store.save_active(workdir, bundle, history_reason="unit_test")
    reloaded = weights_store.load_active(workdir)
    assert reloaded.weights["category_security"] == 9.99
    assert reloaded.generated_by == "test"
    assert reloaded.generated_at > 0.0


def test_save_active_appends_history(tmp_path: Path) -> None:
    workdir = str(tmp_path)
    weights_store.bootstrap(workdir)
    assert weights_store.history_count(workdir) == 0

    b1 = weights_store.load_active(workdir)
    b1.weights["fp_risk_high"] = -3.0
    weights_store.save_active(workdir, b1, history_reason="first")
    assert weights_store.history_count(workdir) == 1

    b2 = weights_store.load_active(workdir)
    b2.weights["fp_risk_high"] = -2.5
    weights_store.save_active(workdir, b2, history_reason="second")
    assert weights_store.history_count(workdir) == 2

    history_path = tmp_path / ".cli-proxy" / "lint_evolution" / "rules" / "weights_history.yaml"
    data = yaml.safe_load(history_path.read_text(encoding="utf-8")) or {}
    history = data.get("history") or []
    reasons = [entry.get("reason") for entry in history]
    assert reasons == ["first", "second"]
    assert history[1]["weights"]["fp_risk_high"] == -2.5


def test_load_active_bootstraps_when_missing(tmp_path: Path) -> None:
    workdir = str(tmp_path)
    bundle = weights_store.load_active(workdir)
    assert bundle.weights, "expected non-empty weights from bundled template"
    active = tmp_path / ".cli-proxy" / "lint_evolution" / "rules" / "decision_weights.yaml"
    assert active.exists()
