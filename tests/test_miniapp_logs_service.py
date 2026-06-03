"""Tests for miniapp/services/logs_service.py — T4 backward-compat checks."""
from types import SimpleNamespace
from unittest.mock import patch

from miniapp.services.logs_service import LogsService


def _make_service(tmp_path):
    app = SimpleNamespace(config=SimpleNamespace(defaults=SimpleNamespace(log_path=str(tmp_path / "bot.log"))))
    return LogsService(app=app)


def test_list_log_types_preserves_label_field(tmp_path) -> None:
    svc = _make_service(tmp_path)
    with patch.object(svc, "_log_paths", return_value={}):
        types = svc.list_log_types()
    assert len(types) > 0
    expected_labels = {
        "main": "Основной",
        "error": "Ошибки",
        "agent": "Agent",
        "cli_dialog": "CLI диалог",
        "miniapp": "MiniApp",
    }
    for item in types:
        assert "id" in item, f"item missing 'id': {item}"
        assert "label" in item, f"item missing 'label': {item}"
        key = item["id"]
        if key in expected_labels:
            assert item["label"] == expected_labels[key], (
                f"label mismatch for {key!r}: got {item['label']!r}, expected {expected_labels[key]!r}"
            )
