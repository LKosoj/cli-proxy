from __future__ import annotations

import pytest

from modes.sdd.ears import parse_ears, parse_ears_block, validate_ears


# ---------------------------------------------------------------------------
# parse_ears — pattern detection
# ---------------------------------------------------------------------------


def test_event_pattern_when():
    c = parse_ears("WHEN the user clicks Submit the system shall save the form")
    assert c.pattern == "event"


def test_event_pattern_when_case_insensitive():
    c = parse_ears("when the user logs in THE SYSTEM SHALL redirect to dashboard")
    assert c.pattern == "event"


def test_state_pattern_while():
    c = parse_ears("WHILE the user is authenticated the system shall show the menu")
    assert c.pattern == "state"


def test_state_pattern_while_lowercase():
    c = parse_ears("while processing the system shall display a spinner")
    assert c.pattern == "state"


def test_optional_pattern_where():
    c = parse_ears("WHERE the feature flag is enabled the system shall show beta UI")
    assert c.pattern == "optional"


def test_optional_pattern_where_lowercase():
    c = parse_ears("where dark mode is active the system shall use dark colours")
    assert c.pattern == "optional"


def test_unwanted_pattern_if_then():
    c = parse_ears("IF the connection fails THEN the system shall retry up to 3 times")
    assert c.pattern == "unwanted"


def test_unwanted_pattern_if_then_lowercase():
    c = parse_ears("if the token is expired then the system shall return 401")
    assert c.pattern == "unwanted"


def test_ubiquitous_shall():
    c = parse_ears("The system SHALL log all API requests")
    assert c.pattern == "ubiquitous"


def test_ubiquitous_the_system_shall():
    c = parse_ears("THE SYSTEM SHALL validate input before saving")
    assert c.pattern == "ubiquitous"


def test_unknown_pattern_plain_text():
    c = parse_ears("The feature must be fast")
    assert c.pattern == "unknown"


def test_unknown_pattern_empty():
    c = parse_ears("")
    assert c.pattern == "unknown"


# ---------------------------------------------------------------------------
# needs_clarification
# ---------------------------------------------------------------------------


def test_needs_clarification_marker():
    c = parse_ears("The system SHALL do something [NEEDS CLARIFICATION]")
    assert c.needs_clarification is True


def test_needs_clarification_case_insensitive():
    c = parse_ears("WHEN something [needs clarification] the system SHALL react")
    assert c.needs_clarification is True


def test_no_clarification_marker():
    c = parse_ears("WHEN the user submits the system SHALL save")
    assert c.needs_clarification is False


# ---------------------------------------------------------------------------
# req_id binding
# ---------------------------------------------------------------------------


def test_req_id_leading():
    c = parse_ears("REQ-3: WHEN the user logs in the system SHALL redirect")
    assert c.req_id == "REQ-3"


def test_req_id_trailing_parens():
    c = parse_ears("WHEN the user logs in the system SHALL redirect (REQ-5)")
    assert c.req_id == "REQ-5"


def test_req_id_none_when_absent():
    c = parse_ears("The system SHALL validate input")
    assert c.req_id is None


def test_req_id_leading_multidigit():
    c = parse_ears("REQ-12: IF network fails THEN the system SHALL retry")
    assert c.req_id == "REQ-12"


# ---------------------------------------------------------------------------
# parse_ears_block — extraction from spec.md
# ---------------------------------------------------------------------------

_SPEC_MD = """\
# Specification: login-feature

## Requirements

- REQ-1: User can log in
- REQ-2: System validates credentials

## Acceptance Criteria

- REQ-1: WHEN the user submits credentials the system SHALL authenticate
- REQ-2: IF credentials are invalid THEN the system SHALL return an error
- THE SYSTEM SHALL log all authentication attempts
- This is just some free text
"""


def test_parse_ears_block_extracts_ac_lines():
    criteria = parse_ears_block(_SPEC_MD)
    assert len(criteria) >= 2
    patterns = {c.pattern for c in criteria}
    assert "event" in patterns or "ubiquitous" in patterns


def test_parse_ears_block_event_criterion():
    criteria = parse_ears_block(_SPEC_MD)
    events = [c for c in criteria if c.pattern == "event"]
    assert len(events) >= 1


def test_parse_ears_block_unwanted_criterion():
    criteria = parse_ears_block(_SPEC_MD)
    unwanted = [c for c in criteria if c.pattern == "unwanted"]
    assert len(unwanted) >= 1


def test_parse_ears_block_req_id_binding():
    criteria = parse_ears_block(_SPEC_MD)
    req_ids = {c.req_id for c in criteria if c.req_id}
    assert "REQ-1" in req_ids or "REQ-2" in req_ids


# ---------------------------------------------------------------------------
# validate_ears
# ---------------------------------------------------------------------------


def test_validate_ears_empty_spec_is_valid():
    problems = validate_ears("# Spec\n\n## Acceptance Criteria\n\n")
    assert problems == []


def test_validate_ears_unknown_ac_is_problem():
    spec = "# Spec\n\n## Acceptance Criteria\n\n- The system must be fast\n"
    problems = validate_ears(spec)
    assert any("паттерна" in p or "unknown" in p.lower() or "EARS" in p or "паттерн" in p for p in problems)


def test_validate_ears_clarification_is_problem():
    spec = (
        "# Spec\n\n## Acceptance Criteria\n\n"
        "- WHEN user logs in the system SHALL authenticate [NEEDS CLARIFICATION]\n"
    )
    problems = validate_ears(spec)
    assert any("уточнения" in p or "clarification" in p.lower() for p in problems)


def test_validate_ears_valid_ears_no_problems():
    spec = (
        "# Spec\n\n## Acceptance Criteria\n\n"
        "- WHEN the user submits the system SHALL save\n"
        "- THE SYSTEM SHALL log all requests\n"
    )
    problems = validate_ears(spec)
    # Should not flag event or ubiquitous patterns without clarification marker
    clarification_problems = [p for p in problems if "уточнения" in p]
    assert clarification_problems == []


# ---------------------------------------------------------------------------
# W6: unwanted takes priority over event/state/optional in combined strings
# ---------------------------------------------------------------------------


def test_combined_when_if_then_unwanted_wins():
    """WHEN ... IF ... THEN ... should be classified as unwanted, not event (W6)."""
    c = parse_ears("WHEN the user is active IF the session expires THEN the system SHALL log out")
    assert c.pattern == "unwanted"


def test_combined_while_if_then_unwanted_wins():
    """WHILE ... IF ... THEN ... should be classified as unwanted, not state (W6)."""
    c = parse_ears("WHILE processing IF an error occurs THEN the system SHALL abort")
    assert c.pattern == "unwanted"


# ---------------------------------------------------------------------------
# frozen dataclass
# ---------------------------------------------------------------------------


def test_ears_criterion_is_frozen():
    c = parse_ears("WHEN user clicks the system SHALL respond")
    with pytest.raises((AttributeError, TypeError)):
        c.pattern = "something_else"  # type: ignore[misc]
