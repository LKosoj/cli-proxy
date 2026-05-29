from __future__ import annotations

from pathlib import Path

from app.services.lint_evolution.signals_ingestor import collect_signals, parse_review_file

_REVIEW_NEGATIVE = """# Review Result [TX]

**Timestamp:** 2026-04-15 12:34:56

---

{
  "approved": false,
  "summary": "Тесты не проходят: падает test_x.",
  "comments": "Тест падает, потому что ожидаемый лог отсутствует в caplog.",
  "tests_passed": false,
  "files_reviewed": ["tests/test_x.py"],
  "not_done_assessment": [
    {
      "item": "Каждый критерий приёмки выполнен",
      "why_not": "Pytest содержит 1 failing.",
      "verdict": "not_justified",
      "comment": "Падающий тест критичен."
    }
  ]
}
"""

_REVIEW_APPROVED = """# Review Result [TY]

**Timestamp:** 2026-04-15 12:34:56

---

{
  "approved": true,
  "summary": "Все тесты проходят.",
  "comments": "",
  "tests_passed": true,
  "files_reviewed": [],
  "not_done_assessment": []
}
"""


def test_parse_negative_yields_multiple_signals(tmp_path: Path) -> None:
    f = tmp_path / "20260415_123456_manager_review_result_TX.md"
    f.write_text(_REVIEW_NEGATIVE, encoding="utf-8")
    items = list(parse_review_file(f))
    assert len(items) >= 3
    texts = [t for t, _ in items]
    assert any("Тест падает" in t for t in texts)
    assert any("Pytest содержит" in t for t in texts)
    assert any("критичен" in t for t in texts)


def test_parse_approved_yields_nothing(tmp_path: Path) -> None:
    f = tmp_path / "20260415_123456_manager_review_result_TY.md"
    f.write_text(_REVIEW_APPROVED, encoding="utf-8")
    assert list(parse_review_file(f)) == []


def test_collect_signals_returns_classified_records(tmp_path: Path) -> None:
    review_dir = tmp_path / ".cli-proxy" / ".manager" / "response"
    review_dir.mkdir(parents=True)
    (review_dir / "20260415_120000_manager_review_result_TA.md").write_text(_REVIEW_NEGATIVE, encoding="utf-8")
    (review_dir / "20260415_130000_manager_review_result_TB.md").write_text(_REVIEW_APPROVED, encoding="utf-8")

    signals, stats = collect_signals(
        project_id="proj",
        project_root=tmp_path,
        glob_patterns=[".cli-proxy/.manager/response/*_manager_review_result_*.md"],
    )
    assert stats.files_seen == 2
    assert all(s.project_id == "proj" for s in signals)
    assert any(s.rule_kind == "tests_failing" for s in signals)
    assert all(s.weight == 3.0 for s in signals)


def test_collect_signals_respects_since_ts(tmp_path: Path) -> None:
    review_dir = tmp_path / ".cli-proxy" / ".manager" / "response"
    review_dir.mkdir(parents=True)
    (review_dir / "20260415_120000_manager_review_result_TA.md").write_text(_REVIEW_NEGATIVE, encoding="utf-8")
    signals, _ = collect_signals(
        project_id="proj",
        project_root=tmp_path,
        glob_patterns=[".cli-proxy/.manager/response/*_manager_review_result_*.md"],
        since_ts=9.9e10,
    )
    assert signals == []
