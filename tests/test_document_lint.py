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


def test_lint_markdown_document_reports_broken_relative_link_with_base_dir(tmp_path) -> None:
    result = lint_markdown_document("[Missing](docs/missing.md)\n", base_dir=tmp_path)
    assert "broken_local_markdown_link: line 1: docs/missing.md" in result["issues"]


def test_lint_markdown_document_accepts_existing_relative_link(tmp_path) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "guide.md").write_text("# Guide\n")

    result = lint_markdown_document("[Guide](docs/guide.md)\n", base_dir=tmp_path)

    assert result["issues"] == []


def test_lint_markdown_document_ignores_local_links_without_base_dir() -> None:
    result = lint_markdown_document("[Missing](docs/missing.md)\n")
    assert result["issues"] == []


def test_lint_markdown_document_ignores_non_file_and_fenced_links(tmp_path) -> None:
    raw = "\n".join(
        [
            "[Web](https://example.com)",
            "[Mail](mailto:test@example.com)",
            "[Anchor](#details)",
            "```md",
            "[Missing](docs/missing.md)",
            "```",
        ]
    )

    result = lint_markdown_document(raw, base_dir=tmp_path)

    assert result["issues"] == []


def test_lint_markdown_document_handles_fragment_title_and_encoded_path(tmp_path) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "file name.md").write_text("# File\n")

    result = lint_markdown_document(
        '[Guide](<docs/file%20name.md?raw=1#section> "Guide title")',
        base_dir=tmp_path,
    )

    assert result["issues"] == []


def test_lint_markdown_document_handles_absolute_path_with_line_suffix(tmp_path) -> None:
    source = tmp_path / "app.py"
    source.write_text("print('ok')\n")

    result = lint_markdown_document(f"[Source]({source}:12:3)\n", base_dir=tmp_path)

    assert result["issues"] == []


def test_lint_markdown_document_checks_reference_definitions(tmp_path) -> None:
    result = lint_markdown_document("[guide]: docs/missing.md\n", base_dir=tmp_path)
    assert "broken_local_markdown_link: line 1: docs/missing.md" in result["issues"]


def test_repair_markdown_document_does_not_repair_broken_links(tmp_path) -> None:
    raw = "[Missing](docs/missing.md)\n"

    lint_result = lint_markdown_document(raw, base_dir=tmp_path)
    repaired, repairs = repair_markdown_document(raw, base_dir=tmp_path)

    assert "broken_local_markdown_link: line 1: docs/missing.md" in lint_result["issues"]
    assert repaired == raw
    assert repairs == []
