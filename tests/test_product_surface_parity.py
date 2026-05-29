from pathlib import Path


DOC_PATH = Path("docs/product-surface-parity.md")

REQUIRED_FEATURES = [
    "session create/select/close",
    "active mode selection",
    "direct CLI input",
    "Agent mode controls",
    "Analyst mode controls",
    "Manager mode controls",
    "Webmaster mode controls",
    "Admin mode controls",
    "files browse/edit/delete/download",
    "logs history/download/stream",
    "run operations: doctor/recover/resume/apply recommendation/promote skills",
    "scheduler CRUD/run now",
    "SSH hosts CRUD/test/keygen/secrets",
    "config editor",
    "git status/diff/commit/pull/push",
    "output delivery",
]

SOURCE_PATH_MARKERS = (
    ".py",
    "README.md",
    "README_EN.MD",
)


def _table_rows(text: str) -> list[list[str]]:
    rows: list[list[str]] = []
    for line in text.splitlines():
        if not line.startswith("| "):
            continue
        if line.startswith("|---"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if cells and cells[0] != "Feature":
            rows.append(cells)
    return rows


def test_product_surface_parity_matrix_contract() -> None:
    assert DOC_PATH.exists()
    text = DOC_PATH.read_text(encoding="utf-8")

    assert "| Feature | Telegram Bot | MiniApp | Desktop | Source of truth | Notes / intentional gaps |" in text
    assert "unsupported" in text
    assert "must match" in text
    assert "decision required" in text
    assert "Webmaster mode controls" in text
    assert "inventory only" in text
    assert "no hidden Webmaster migration" in text

    rows = _table_rows(text)
    rows_by_feature = {row[0]: row for row in rows}
    for feature in REQUIRED_FEATURES:
        assert feature in rows_by_feature

    for feature in REQUIRED_FEATURES:
        row = rows_by_feature[feature]
        assert len(row) == 6
        source_cell = row[4]
        assert any(marker in source_cell for marker in SOURCE_PATH_MARKERS), feature

    assert len(rows) == len(REQUIRED_FEATURES)
