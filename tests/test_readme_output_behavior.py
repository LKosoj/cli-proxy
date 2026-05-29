from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def test_readmes_describe_actual_output_delivery_without_always_html() -> None:
    readme_ru = _read("README.md")
    readme_en = _read("README_EN.MD")
    combined = f"{readme_ru}\n{readme_en}"

    forbidden = [
        r"вывод\s+всегда.*html",
        r"output\s+always\s+sent\s+as\s+an\s+html\s+file",
        r"полный\s+результат\s+отправляется\s+html-файлом",
        r"full\s+output\s+is\s+sent\s+as\s+an\s+html\s+file",
    ]
    for pattern in forbidden:
        assert re.search(pattern, combined, flags=re.IGNORECASE | re.DOTALL) is None

    assert "до 3900 символов" in readme_ru
    assert "up to 3900 characters" in readme_en
    assert "`force_html=True`" in readme_ru
    assert "`force_html=True`" in readme_en
    assert "Summary/preview" in readme_ru
    assert "Summary/preview" in readme_en
