from __future__ import annotations

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
_UI_PATH = REPO_ROOT / "modes" / "admin" / "ui.py"
_SPEC = importlib.util.spec_from_file_location("modes_admin_ui_test", _UI_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"failed to load admin ui module from {_UI_PATH}")
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)

build_admin_error_text = _MODULE.build_admin_error_text
build_admin_incidents_screen = _MODULE.build_admin_incidents_screen
build_admin_menu_text = _MODULE.build_admin_menu_text
build_admin_approval_prompt = _MODULE.build_admin_approval_prompt
build_admin_status_text = _MODULE.build_admin_status_text
merge_menu_with_note = _MODULE.merge_menu_with_note


def test_admin_menu_text_uses_markdownv2_and_escapes_dynamic_session_id() -> None:
    text = build_admin_menu_text(session_id="s_[1](test)!-x", active=True)
    assert "*🛡 Admin Mode*" in text
    assert "s\\_\\[1\\]\\(test\\)\\!\\-x" in text
    assert "Состояние" in text


def test_admin_status_text_uses_markdownv2_and_escapes_payload_fields() -> None:
    payload = {
        "session_id": "s_[42](x)!",
        "active": True,
        "busy": False,
        "run_lock_locked": True,
        "tick_active": False,
        "mode_tasks_running": True,
    }
    text = build_admin_status_text(payload)
    assert "*🛡 Admin статус*" in text
    assert "s\\_\\[42\\]\\(x\\)\\!" in text
    assert "*Busy 3sig:* да" in text
    assert "*Mode tasks:* есть" in text


def test_merge_menu_with_note_escapes_note() -> None:
    merged = merge_menu_with_note(menu_text="*menu*", note="enabled (ok)!")
    assert "_📌 enabled \\(ok\\)\\!_" in merged
    assert "*menu*" in merged


def test_admin_error_text_is_markdownv2_safe() -> None:
    text = build_admin_error_text("schema init failed (admin)!")
    assert "*🛡 Admin*" in text
    assert "schema init failed \\(admin\\)\\!" in text


def test_admin_approval_prompt_includes_risk_and_confidence() -> None:
    text = build_admin_approval_prompt(
        action="restart",
        confidence=0.42,
        diagnosis="502_with_php_fpm_down",
        reason="rule_engine:detected_502_and_php_fpm_down",
        triggers=["risky_action", "low_confidence"],
    )
    assert "требуется подтверждение" in text
    assert "рискованное действие" in text
    assert "низкая уверенность Analyzer" in text
    assert "Action: restart" in text
    assert "Confidence: 0.42" in text


def test_admin_incidents_screen_shows_typed_incident_evidence() -> None:
    text = build_admin_incidents_screen(
        incidents=[
            {
                "incident_id": "incident:1",
                "payload": {
                    "decision": {
                        "urgency": "warning",
                        "incident_type": "security.bruteforce_suspected",
                        "diagnosis": "security.bruteforce_suspected",
                        "evidence": [
                            {
                                "source": "rule_engine",
                                "ref": "scan:security:fail2ban:fail2ban_jail_sshd_currently_failed=75",
                            }
                        ],
                    }
                },
            }
        ]
    )

    assert "security\\.bruteforce\\_suspected" in text
    assert "fail2ban\\_jail\\_sshd\\_currently\\_failed\\=75" in text
