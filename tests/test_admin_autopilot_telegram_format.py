from modes.admin.mode import _format_autopilot_exec_summary


def test_propose_action_summary_has_action_and_exit_code():
    text = _format_autopilot_exec_summary(
        intent={"type": "propose_action", "action_id": "check_disk"},
        exec_result={"target_kind": "local", "exit_code": 0, "stdout": "Filesystem"},
    )
    assert "check_disk" in text
    assert "local" in text
    assert "Exit code: 0" in text
    assert "STDOUT" in text


def test_propose_new_action_summary_uses_argv_when_no_action_id():
    text = _format_autopilot_exec_summary(
        intent={"type": "propose_new_action", "argv": ["ls", "-la"]},
        exec_result={"target_kind": "local", "exit_code": 0, "stdout": ""},
    )
    assert "ls -la" in text
    assert "Exit code: 0" in text


def test_propose_plan_summary_reports_completed_steps():
    text = _format_autopilot_exec_summary(
        intent={"type": "propose_plan"},
        exec_result={
            "target_kind": "plan",
            "total_steps": 3,
            "completed_steps": 3,
            "stopped_early": False,
        },
    )
    assert "Autopilot выполнил plan" in text
    assert "3/3" in text


def test_propose_plan_summary_reports_stopped_early():
    text = _format_autopilot_exec_summary(
        intent={"type": "propose_plan"},
        exec_result={
            "target_kind": "plan",
            "total_steps": 3,
            "completed_steps": 2,
            "stopped_early": True,
        },
    )
    assert "остановил plan" in text
    assert "2/3" in text


def test_propose_plan_summary_includes_runbook_id_if_saved():
    text = _format_autopilot_exec_summary(
        intent={"type": "propose_plan"},
        exec_result={
            "target_kind": "plan",
            "total_steps": 1,
            "completed_steps": 1,
            "stopped_early": False,
            "runbook_saved": True,
            "runbook_id": "disk-cleanup",
        },
    )
    assert "disk-cleanup" in text


def test_large_stdout_is_truncated():
    big_stdout = "x" * 2000
    text = _format_autopilot_exec_summary(
        intent={"type": "propose_action", "action_id": "noisy"},
        exec_result={"target_kind": "local", "exit_code": 0, "stdout": big_stdout},
    )
    assert "…" in text
    assert "xxxxxxx" in text
