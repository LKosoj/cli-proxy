from __future__ import annotations

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
_UI_PATH = REPO_ROOT / "modes" / "admin" / "ui.py"
_SPEC = importlib.util.spec_from_file_location("modes_admin_ui_test_runbooks", _UI_PATH)
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)

build_admin_runbook_validation_screen = _MODULE.build_admin_runbook_validation_screen
build_admin_runbook_promote_screen = _MODULE.build_admin_runbook_promote_screen


def test_validation_screen_ok_report():
    text = build_admin_runbook_validation_screen(
        rb_id="rb-x",
        report={
            "ok": True,
            "checks": [{"step": "01-run", "checksum": "ok", "bash_n": "ok", "shellcheck": "skipped"}],
            "errors": [],
            "warnings": [],
        },
    )
    assert "*✅ Validate `rb\\-x`*" in text
    assert "`01\\-run`" in text


def test_validation_screen_failing_report_lists_errors():
    text = build_admin_runbook_validation_screen(
        rb_id="rb-y",
        report={
            "ok": False,
            "checks": [],
            "errors": ["boom: checksum mismatch"],
            "warnings": ["heads up"],
        },
    )
    assert "❌" in text
    assert "boom: checksum mismatch" in text
    assert "heads up" in text


def test_promote_screen_renders_added_and_confidence():
    text = build_admin_runbook_promote_screen(
        rb_id="rb-z",
        result={
            "added_servers": ["prod-01", "prod-02"],
            "already_present": ["prod-03"],
            "confidence_before": 0.0,
            "confidence_after": 0.8,
            "validation": {"ok": True},
        },
    )
    assert "`rb\\-z`" in text
    assert "prod\\-01" in text
    assert "*already present:*" in text
    assert "✅ ok" in text
