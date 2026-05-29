from pathlib import Path

from app.services.run_utils import MISSING, as_list_of_strings, clean_optional_text, clean_text, nested_get


def test_run_utils_contract_covers_text_lists_and_nested_paths() -> None:
    assert clean_text("line1\nline2", max_len=32) == "line1 line2"
    assert clean_optional_text("   ", max_len=32) is None
    assert as_list_of_strings(["one", "one", "", "two"]) == ["one", "two"]
    payload = {"mode_context": {"result": {"status": "ok"}}}
    assert nested_get(payload, "mode_context.result.status") == "ok"
    assert nested_get(payload, "mode_context.result.missing") is MISSING


def test_run_utils_helpers_are_not_duplicated_in_boundary_and_doctor_services() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    doctor_text = (repo_root / "app/services/run_doctor_service.py").read_text(encoding="utf-8")
    boundary_text = (repo_root / "app/services/run_boundary_validation_service.py").read_text(encoding="utf-8")

    for token in (
        "def _clean_text(",
        "def _clean_optional_text(",
        "def _as_list_of_strings(",
        "def _nested_get(",
    ):
        assert token not in doctor_text
        assert token not in boundary_text

    assert "from app.services.run_utils import (" in doctor_text
    assert "from app.services.run_utils import (" in boundary_text
