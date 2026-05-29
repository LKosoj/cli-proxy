from modes.sdk.runtime.document_lint import (
    lint_markdown_document,
    render_document_lint_report,
    repair_markdown_document,
)


def test_lint_markdown_document_detects_unbalanced_fence() -> None:
    result = lint_markdown_document("## Title\n\n```python\nprint('x')\n")
    assert "unbalanced_fenced_code_blocks" in result["issues"]


def test_repair_markdown_document_closes_unbalanced_fence() -> None:
    repaired, repairs = repair_markdown_document("## Title\n\n```python\nprint('x')\n")
    assert repaired.endswith("\n```\n")
    assert "closed_unbalanced_fenced_code_blocks" in repairs


def test_render_document_lint_report_lists_issues_and_repairs() -> None:
    text = render_document_lint_report(
        issues=["unbalanced_fenced_code_blocks"],
        repairs=["closed_unbalanced_fenced_code_blocks"],
    )
    assert "# Document Lint" in text
    assert "unbalanced_fenced_code_blocks" in text
    assert "closed_unbalanced_fenced_code_blocks" in text


def test_lint_markdown_document_detects_unsafe_raw_html() -> None:
    result = lint_markdown_document('Hello\n<script>alert(1)</script>\n')
    assert "unsafe_raw_html" in result["issues"]


def test_repair_markdown_document_escapes_unsafe_raw_html() -> None:
    repaired, repairs = repair_markdown_document('Hello\n<script>alert(1)</script>\n')
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in repaired
    assert "escaped_unsafe_raw_html" in repairs


def test_lint_markdown_document_detects_malformed_table() -> None:
    raw = "| A | B |\n| --- | --- |\n| 1 |\n"
    result = lint_markdown_document(raw)
    assert "malformed_markdown_table" in result["issues"]


def test_repair_markdown_document_converts_malformed_table_to_list() -> None:
    raw = "| A | B |\n| --- | --- |\n| 1 |\n"
    repaired, repairs = repair_markdown_document(raw)
    assert "- A | B" in repaired or "- 1" in repaired
    assert "converted_malformed_markdown_tables_to_lists" in repairs
