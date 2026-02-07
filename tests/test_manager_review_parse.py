"""Tests for review result parsing (_try_parse_review)."""

from __future__ import annotations

import json

from agent.manager import ManagerOrchestrator


def _make_orchestrator():
    obj = object.__new__(ManagerOrchestrator)
    obj._config = type("C", (), {"defaults": type("D", (), {})()})()
    return obj


class TestTryParseReview:

    def test_valid_approved(self):
        orch = _make_orchestrator()
        text = json.dumps({
            "approved": True,
            "summary": "All good",
            "comments": "",
            "files_reviewed": ["main.py"],
            "tests_passed": True,
        })
        result = orch._try_parse_review(text)
        assert result is not None
        assert result.approved is True
        assert result.summary == "All good"
        assert result.files_reviewed == ["main.py"]
        assert result.tests_passed is True

    def test_valid_rejected(self):
        orch = _make_orchestrator()
        text = json.dumps({
            "approved": False,
            "summary": "Issues found",
            "comments": "Missing tests",
            "files_reviewed": ["app.py"],
            "tests_passed": False,
        })
        result = orch._try_parse_review(text)
        assert result is not None
        assert result.approved is False
        assert result.comments == "Missing tests"

    def test_json_in_markdown(self):
        orch = _make_orchestrator()
        text = '```json\n{"approved": true, "summary": "ok", "comments": ""}\n```'
        result = orch._try_parse_review(text)
        assert result is not None
        assert result.approved is True

    def test_invalid_text(self):
        orch = _make_orchestrator()
        result = orch._try_parse_review("This is just plain text without JSON")
        assert result is None

    def test_missing_approved_key(self):
        orch = _make_orchestrator()
        text = json.dumps({"summary": "ok", "comments": ""})
        result = orch._try_parse_review(text)
        assert result is None

    def test_empty_string(self):
        orch = _make_orchestrator()
        result = orch._try_parse_review("")
        assert result is None

    def test_tests_passed_null(self):
        orch = _make_orchestrator()
        text = json.dumps({
            "approved": True,
            "summary": "ok",
            "comments": "",
            "tests_passed": None,
        })
        result = orch._try_parse_review(text)
        assert result is not None
        assert result.tests_passed is None

    def test_surrounding_text(self):
        orch = _make_orchestrator()
        text = 'Review complete.\n{"approved": false, "summary": "bad", "comments": "fix it"}\nDone.'
        result = orch._try_parse_review(text)
        assert result is not None
        assert result.approved is False
