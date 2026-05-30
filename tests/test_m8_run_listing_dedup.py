"""M8: verify that run-listing serialization helpers are deduplicated into run_utils."""
from pathlib import Path

from app.services.run_utils import clean_text, summarize_run_skill_log


_REPO = Path(__file__).resolve().parents[1]
_MINIAPP = _REPO / "miniapp/routes.py"
_FACADE = _REPO / "desktop/services/application_facade.py"


# ---------------------------------------------------------------------------
# summarize_run_skill_log behaviour
# ---------------------------------------------------------------------------

def test_summarize_empty_returns_empty() -> None:
    assert summarize_run_skill_log([], {}) == []


def test_summarize_fallback_to_state_selected_skills() -> None:
    result = summarize_run_skill_log([], {"selected_skill_ids": ["skill-a", "skill-b"]})
    assert result == ["Injected: skill-a, skill-b"]


def test_summarize_injected_event() -> None:
    events = [{"event_type": "cli_skill_context_applied", "selected_skill_ids": ["foo", "bar"]}]
    result = summarize_run_skill_log(events, {})
    assert result == ["Injected: foo, bar"]


def test_summarize_selected_event() -> None:
    events = [{"event_type": "skill_selection", "selected_skill_ids": ["alpha"]}]
    result = summarize_run_skill_log(events, {})
    assert result == ["Selected: alpha"]


def test_summarize_install_event() -> None:
    events = [{"event_type": "skill_install", "skill_id": "my-skill"}]
    result = summarize_run_skill_log(events, {})
    assert result == ["Installed: my-skill"]


def test_summarize_discovery_event() -> None:
    events = [{"event_type": "skill_discovery", "discovered_skills": ["x", "y"]}]
    result = summarize_run_skill_log(events, {})
    assert result == ["Discovered: x, y"]


def test_summarize_promote_event() -> None:
    events = [{"event_type": "skill_promote_global", "skill_id": "promoted-skill"}]
    result = summarize_run_skill_log(events, {})
    assert result == ["Promoted: promoted-skill"]


def test_summarize_caps_at_three_entries() -> None:
    events = [
        {"event_type": "skill_install", "skill_id": f"skill-{i}"}
        for i in range(10)
    ]
    result = summarize_run_skill_log(events, {})
    assert len(result) == 3


def test_summarize_deduplicates_entries() -> None:
    events = [
        {"event_type": "skill_install", "skill_id": "dup"},
        {"event_type": "skill_install", "skill_id": "dup"},
    ]
    result = summarize_run_skill_log(events, {})
    assert result.count("Installed: dup") == 1


def test_summarize_skips_non_dict_events() -> None:
    events = ["not-a-dict", None, {"event_type": "skill_install", "skill_id": "ok"}]
    result = summarize_run_skill_log(events, {})
    assert result == ["Installed: ok"]


def test_summarize_selected_skills_fallback_with_selected_skills_key() -> None:
    events = [{"event_type": "skill_selection", "selected_skills": ["legacy-skill"]}]
    result = summarize_run_skill_log(events, {})
    assert result == ["Selected: legacy-skill"]


def test_summarize_fallback_truncates_to_four() -> None:
    state = {"selected_skill_ids": [f"s{i}" for i in range(6)]}
    result = summarize_run_skill_log([], state)
    assert len(result) == 1
    assert result[0].startswith("Injected: ")
    assert len(result[0].split(", ")) == 4


# ---------------------------------------------------------------------------
# clean_text contract (same as _clean_run_listing_text)
# ---------------------------------------------------------------------------

def test_clean_text_strips_newlines() -> None:
    assert clean_text("a\nb") == "a b"


def test_clean_text_truncates() -> None:
    long = "x" * 300
    result = clean_text(long, max_len=256)
    assert len(result) == 256
    assert result.endswith("...")


def test_clean_text_none_input() -> None:
    assert clean_text(None) == ""


# ---------------------------------------------------------------------------
# Structural: private duplicates must not exist; imports from run_utils must
# ---------------------------------------------------------------------------

def test_no_private_clean_method_in_miniapp() -> None:
    text = _MINIAPP.read_text(encoding="utf-8")
    assert "def _clean_run_listing_text(" not in text


def test_no_private_summarize_method_in_miniapp() -> None:
    text = _MINIAPP.read_text(encoding="utf-8")
    assert "def _summarize_run_skill_log(" not in text


def test_miniapp_imports_from_run_utils() -> None:
    text = _MINIAPP.read_text(encoding="utf-8")
    assert "from app.services.run_utils import" in text


def test_no_private_clean_method_in_facade() -> None:
    text = _FACADE.read_text(encoding="utf-8")
    assert "def _clean_run_listing_text(" not in text


def test_no_private_summarize_method_in_facade() -> None:
    text = _FACADE.read_text(encoding="utf-8")
    assert "def _summarize_run_skill_log(" not in text


def test_facade_imports_from_run_utils() -> None:
    text = _FACADE.read_text(encoding="utf-8")
    assert "from app.services.run_utils import" in text
