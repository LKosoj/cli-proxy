from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

from jsonschema import Draft202012Validator

REPO_ROOT = Path(__file__).resolve().parents[1]
MODES_ROOT = REPO_ROOT / "modes"
SDK_ROOT = MODES_ROOT / "sdk"
SDK_RUNTIME_ROOT = SDK_ROOT / "runtime"
ADMIN_ROOT = MODES_ROOT / "admin"
_ANALYZER_PATH = ADMIN_ROOT / "analyzer.py"
_SCHEMAS_PATH = ADMIN_ROOT / "schemas.py"
_JSON_NORMALIZER_PATH = SDK_RUNTIME_ROOT / "json_normalizer.py"
_MISSING = object()


def _load_analyzer_module():
    keys = [
        "modes",
        "modes.sdk",
        "modes.sdk.runtime",
        "modes.sdk.runtime.json_normalizer",
        "modes.admin",
        "modes.admin.schemas",
        "modes.admin.analyzer_test",
    ]
    backup = {key: sys.modules.get(key, _MISSING) for key in keys}
    try:
        modes_pkg = types.ModuleType("modes")
        modes_pkg.__path__ = [str(MODES_ROOT)]
        sdk_pkg = types.ModuleType("modes.sdk")
        sdk_pkg.__path__ = [str(SDK_ROOT)]
        runtime_pkg = types.ModuleType("modes.sdk.runtime")
        runtime_pkg.__path__ = [str(SDK_RUNTIME_ROOT)]
        admin_pkg = types.ModuleType("modes.admin")
        admin_pkg.__path__ = [str(ADMIN_ROOT)]

        sys.modules["modes"] = modes_pkg
        sys.modules["modes.sdk"] = sdk_pkg
        sys.modules["modes.sdk.runtime"] = runtime_pkg
        sys.modules["modes.admin"] = admin_pkg

        norm_spec = importlib.util.spec_from_file_location(
            "modes.sdk.runtime.json_normalizer",
            _JSON_NORMALIZER_PATH,
        )
        if norm_spec is None or norm_spec.loader is None:
            raise RuntimeError(f"failed to load json_normalizer module from {_JSON_NORMALIZER_PATH}")
        norm_module = importlib.util.module_from_spec(norm_spec)
        sys.modules[norm_spec.name] = norm_module
        norm_spec.loader.exec_module(norm_module)

        _load_schemas_module(module_name="modes.admin.schemas")

        spec = importlib.util.spec_from_file_location("modes.admin.analyzer_test", _ANALYZER_PATH)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"failed to load analyzer module from {_ANALYZER_PATH}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        for key, value in backup.items():
            if value is _MISSING:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = value


def _load_schemas_module(*, module_name: str = "modes_admin_schemas_test"):
    spec = importlib.util.spec_from_file_location(module_name, _SCHEMAS_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to load schemas module from {_SCHEMAS_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_MODULE = _load_analyzer_module()
_SCHEMAS_MODULE = _load_schemas_module()
AdminAnalyzer = _MODULE.AdminAnalyzer
AdminAnalyzerDecisionSchema = _MODULE.AdminAnalyzerDecisionSchema
SharedAdminAnalyzerDecisionSchema = _SCHEMAS_MODULE.AdminAnalyzerDecisionSchema


def _admin_config_for_generated_rule(
    *,
    rule_id: str,
    runbook_id: str,
    action_id: str,
    command: str,
    thresholds: list[dict[str, object]],
) -> dict[str, object]:
    return {
        "admin": {
            "incidents": {
                "rules": {
                    rule_id: {
                        "id": f"rule:{rule_id}",
                        "service": "generated:test",
                        "check_id": "check:generated:test",
                        "runbook_id": runbook_id,
                        "thresholds": thresholds,
                        "fallback_action": "notify_admin",
                    }
                }
            },
            "runbooks": {
                "templates": {
                    runbook_id: {
                        "id": runbook_id,
                        "steps": [
                            {
                                "name": "inspect",
                                "target": "local",
                                "action_id": action_id,
                            }
                        ],
                    }
                }
            },
            "actions": {
                "local": {
                    action_id: {
                        "argv": ["bash", "-lc", command],
                        "timeout_sec": 45,
                        "risk_level": "low",
                    }
                }
            },
        }
    }


def test_admin_analyzer_returns_valid_contract_for_valid_json() -> None:
    analyzer = AdminAnalyzer()
    raw = (
        '{"diagnosis":"cpu saturation",'
        '"confidence":"high",'
        '"action":"clear_tmp",'
        '"reason":"cpu > 95% for 5m",'
        '"urgency":"warning"}'
    )
    result = analyzer.analyze_llm_output(raw)

    Draft202012Validator(AdminAnalyzerDecisionSchema).validate(result)
    assert result["action"] == "clear_tmp"
    assert result["urgency"] == "warning"
    assert result["confidence"] == "high"


def test_admin_analyzer_returns_notify_admin_on_invalid_json() -> None:
    analyzer = AdminAnalyzer()
    result = analyzer.analyze_llm_output("not a json document")

    Draft202012Validator(AdminAnalyzerDecisionSchema).validate(result)
    assert result["action"] == "notify_admin"
    assert result["reason"] == "invalid_json_or_schema"


def test_admin_analyzer_returns_notify_admin_on_schema_validation_error() -> None:
    analyzer = AdminAnalyzer()
    raw = (
        '{"diagnosis":"cpu saturation",'
        '"confidence":"high",'
        '"action":"run_remediation",'
        '"reason":"cpu > 95% for 5m",'
        '"urgency":"critical"}'
    )
    result = analyzer.analyze_llm_output(raw)

    Draft202012Validator(AdminAnalyzerDecisionSchema).validate(result)
    assert result["action"] == "notify_admin"
    assert result["reason"] == "invalid_json_or_schema"


def test_admin_shared_analyzer_schema_contains_optional_secondary_cli_command() -> None:
    properties = dict(SharedAdminAnalyzerDecisionSchema.get("properties") or {})
    required = list(SharedAdminAnalyzerDecisionSchema.get("required") or [])

    assert "secondary_cli_command" in properties
    assert properties["secondary_cli_command"]["type"] == "string"
    assert "secondary_cli_command" not in required


def test_admin_analyzer_source_removes_service_specific_hardcoded_strings() -> None:
    source = _ANALYZER_PATH.read_text(encoding="utf-8")
    assert "restart_php_fpm" not in source
    assert "restart_postgresql" not in source


def test_admin_analyzer_service_specific_php_snapshot_now_uses_llm_fallback() -> None:
    analyzer = AdminAnalyzer()
    snapshot = {
        "servers": [
            {"server_id": "web-1", "metrics": {"http_status": 502}},
            {"server_id": "php-1", "metrics": {"php_fpm_state": "down"}},
        ]
    }
    llm_raw = (
        '{"diagnosis":"llm_selected_cleanup",'
        '"confidence":"high",'
        '"action":"clear_tmp",'
        '"reason":"llm_fallback_selected",'
        '"urgency":"warning"}'
    )
    result = analyzer.analyze(snapshot=snapshot, llm_output=llm_raw)

    Draft202012Validator(AdminAnalyzerDecisionSchema).validate(result)
    assert result["action"] == "clear_tmp"
    assert result["diagnosis"] == "llm_selected_cleanup"
    assert result["reason"] == "llm_fallback_selected"


def test_admin_analyzer_preserves_optional_secondary_cli_command_from_llm() -> None:
    analyzer = AdminAnalyzer()
    raw = (
        '{"diagnosis":"need_more_data",'
        '"confidence":"low",'
        '"action":"notify_admin",'
        '"reason":"secondary_check_required",'
        '"urgency":"warning",'
        '"secondary_cli_command":"systemctl status nginx --no-pager"}'
    )

    result = analyzer.analyze_llm_output(raw)

    Draft202012Validator(AdminAnalyzerDecisionSchema).validate(result)
    assert result["secondary_cli_command"] == "systemctl status nginx --no-pager"
    assert result["action"] == "notify_admin"


def test_admin_analyzer_generates_cli_step_for_low_confidence_service_case() -> None:
    analyzer = AdminAnalyzer()
    snapshot = {
        "servers": [
            {"server_id": "web-1", "metrics": {"http_status": 502}},
        ]
    }
    admin_config = {
        "admin": {
            "environment": {
                "services": {
                    "nginx": {
                        "category": "web_stack",
                        "transport": "local",
                    }
                }
            }
        }
    }
    llm_raw = (
        '{"diagnosis":"upstream_unknown",'
        '"confidence":"low",'
        '"action":"notify_admin",'
        '"reason":"insufficient_data_for_service_specific_root_cause",'
        '"urgency":"warning"}'
    )

    result = analyzer.analyze(snapshot=snapshot, llm_output=llm_raw, admin_config=admin_config)

    Draft202012Validator(AdminAnalyzerDecisionSchema).validate(result)
    assert result["action"] == "notify_admin"
    assert "secondary_cli_command" in result
    assert "nginx" in result["secondary_cli_command"]


def test_admin_analyzer_service_specific_db_snapshot_now_uses_llm_fallback() -> None:
    analyzer = AdminAnalyzer()
    snapshot = {
        "servers": [
            {"server_id": "db-1", "metrics": {"postgresql_state": "down"}},
        ]
    }
    result = analyzer.analyze(
        snapshot=snapshot,
        llm_output=(
            '{"diagnosis":"llm_selected_notify",'
            '"confidence":"medium",'
            '"action":"notify_admin",'
            '"reason":"db_needs_manual_review",'
            '"urgency":"critical"}'
        ),
    )

    Draft202012Validator(AdminAnalyzerDecisionSchema).validate(result)
    assert result["action"] == "notify_admin"
    assert result["diagnosis"] == "llm_selected_notify"
    assert result["reason"] == "db_needs_manual_review"


def test_admin_analyzer_rule_engine_detects_disk_high_and_recommends_cleanup() -> None:
    analyzer = AdminAnalyzer()
    snapshot = {
        "servers": [
            {"server_id": "srv-1", "metrics": {"disk_usage_pct": 95}},
        ]
    }

    result = analyzer.analyze(snapshot=snapshot, llm_output="")
    Draft202012Validator(AdminAnalyzerDecisionSchema).validate(result)
    assert result["action"] == "clear_logs"
    assert result["diagnosis"] == "disk_high"
    assert result["urgency"] == "warning"


def test_admin_analyzer_rule_engine_returns_no_action_for_healthy_snapshot() -> None:
    analyzer = AdminAnalyzer()
    snapshot = {
        "servers": [
            {
                "server_id": "srv-1",
                "target": "ssh",
                "metrics": {
                    "host_alive": True,
                    "uptime_seconds": 12345,
                    "disk_root_pct": 42,
                },
                "returncode": 0,
                "timed_out": False,
            },
        ],
    }

    result = analyzer.analyze(snapshot=snapshot, llm_output="")

    Draft202012Validator(AdminAnalyzerDecisionSchema).validate(result)
    assert result["action"] == "no_action"
    assert result["diagnosis"] == "healthy"
    assert result["confidence"] == "high"
    assert "secondary_cli_command" not in result


def test_admin_analyzer_rule_engine_detects_process_down_without_llm() -> None:
    analyzer = AdminAnalyzer()
    snapshot = {
        "servers": [
            {
                "server_id": "scan:process:fail2ban-server",
                "metrics": {
                    "process_fail2ban_server_state": "down",
                    "process_fail2ban_server_count": 0,
                },
                "returncode": 0,
                "timed_out": False,
            },
        ],
    }

    result = analyzer.analyze(snapshot=snapshot, llm_output="")

    Draft202012Validator(AdminAnalyzerDecisionSchema).validate(result)
    assert result["action"] == "notify_admin"
    assert result["diagnosis"] == "process_down"
    assert result["confidence"] == "high"
    assert result["incident_type"] == "availability.process_down"
    assert result["evidence"][0]["ref"].endswith("process_fail2ban_server_state=down")
    assert result["suggested_runbook_ids"] == ["inspect_process_fail2ban_server"]
    assert "unable_to_parse_llm_response" not in result["diagnosis"]
    assert "process_fail2ban_server_state=down" in result["reason"]


def test_admin_analyzer_rule_engine_detects_process_down_after_invalid_llm() -> None:
    analyzer = AdminAnalyzer()
    snapshot = {
        "servers": [
            {
                "server_id": "scan:process:fail2ban-server",
                "metrics": {
                    "process_fail2ban_server_state": "down",
                    "process_fail2ban_server_count": 0,
                },
                "returncode": 0,
                "timed_out": False,
            },
        ],
    }

    result = analyzer.analyze(snapshot=snapshot, llm_output="not-json")

    Draft202012Validator(AdminAnalyzerDecisionSchema).validate(result)
    assert result["action"] == "notify_admin"
    assert result["diagnosis"] == "process_down"
    assert result["confidence"] == "high"
    assert result["incident_type"] == "availability.process_down"
    assert result["reason"].startswith("rule_engine:process_down:")


def test_admin_analyzer_rule_engine_detects_unhealthy_state_without_llm() -> None:
    analyzer = AdminAnalyzer()
    snapshot = {
        "servers": [
            {
                "server_id": "scan:docker_container:app",
                "metrics": {
                    "docker_container_app_state": "running",
                    "docker_container_app_health": "unhealthy",
                },
                "returncode": 0,
                "timed_out": False,
            },
        ],
    }

    result = analyzer.analyze(snapshot=snapshot, llm_output="")

    Draft202012Validator(AdminAnalyzerDecisionSchema).validate(result)
    assert result["action"] == "notify_admin"
    assert result["diagnosis"] == "service_unhealthy"
    assert result["confidence"] == "high"
    assert result["incident_type"] == "availability.service_unhealthy"
    assert "docker_container_app_health=unhealthy" in result["reason"]


def test_admin_analyzer_rule_engine_does_not_treat_unknown_state_as_failure() -> None:
    analyzer = AdminAnalyzer()
    snapshot = {
        "servers": [
            {
                "server_id": "scan:security:fail2ban",
                "metrics": {
                    "fail2ban_state": "unknown",
                    "fail2ban_jail_count": 0,
                },
                "returncode": 0,
                "timed_out": False,
            },
        ],
    }

    result = analyzer.analyze(snapshot=snapshot, llm_output="")

    Draft202012Validator(AdminAnalyzerDecisionSchema).validate(result)
    assert result["action"] == "no_action"
    assert result["diagnosis"] == "healthy"


def test_admin_analyzer_rule_engine_does_not_treat_false_diagnostic_flag_as_failure() -> None:
    analyzer = AdminAnalyzer()
    snapshot = {
        "servers": [
            {
                "server_id": "scan:docker_container:app",
                "metrics": {
                    "docker_container_app_state": "running",
                    "docker_container_app_health": "none",
                    "docker_container_app_oom_killed": False,
                },
                "returncode": 0,
                "timed_out": False,
            },
        ],
    }

    result = analyzer.analyze(snapshot=snapshot, llm_output="")

    Draft202012Validator(AdminAnalyzerDecisionSchema).validate(result)
    assert result["action"] == "no_action"
    assert result["diagnosis"] == "healthy"


def test_admin_analyzer_rule_engine_detects_monitor_check_failure_without_llm() -> None:
    analyzer = AdminAnalyzer()
    snapshot = {
        "servers": [
            {
                "server_id": "scan:process:fail2ban-server",
                "metrics": {},
                "returncode": 127,
                "timed_out": False,
                "error": "command not found",
            },
        ],
    }

    result = analyzer.analyze(snapshot=snapshot, llm_output="")

    Draft202012Validator(AdminAnalyzerDecisionSchema).validate(result)
    assert result["action"] == "notify_admin"
    assert result["diagnosis"] == "monitor_check_failed"
    assert result["confidence"] == "medium"
    assert result["incident_type"] == "monitor.check_failed"
    assert "command not found" in result["reason"] or "returncode=127" in result["reason"]


def test_admin_analyzer_rule_engine_detects_fail2ban_security_signal() -> None:
    analyzer = AdminAnalyzer()
    admin_config = _admin_config_for_generated_rule(
        rule_id="fail2ban_security_activity",
        runbook_id="inspect_security_fail2ban",
        action_id="inspect_security_fail2ban",
        command="fail2ban-client status || true",
        thresholds=[
            {
                "metric_suffix": "_currently_failed",
                "op": "gte",
                "value": 50,
                "incident_type": "security.bruteforce_suspected",
                "risk_level": "high",
            }
        ],
    )
    snapshot = {
        "servers": [
            {
                "server_id": "scan:security:fail2ban",
                "metrics": {
                    "fail2ban_state": "running",
                    "fail2ban_jail_sshd_currently_failed": 75,
                    "fail2ban_jail_sshd_currently_banned": 12,
                },
                "returncode": 0,
                "timed_out": False,
            },
        ],
    }

    result = analyzer.analyze(snapshot=snapshot, llm_output="", admin_config=admin_config)

    Draft202012Validator(AdminAnalyzerDecisionSchema).validate(result)
    assert result["action"] == "notify_admin"
    assert result["diagnosis"] == "security.bruteforce_suspected"
    assert result["incident_type"] == "security.bruteforce_suspected"
    assert result["risk_level"] == "high"
    assert result["suggested_runbook_ids"] == ["inspect_security_fail2ban"]
    assert result["secondary_cli_command"] == "fail2ban-client status || true"
    assert "fail2ban_jail_sshd_currently_failed=75" in result["reason"]


def test_admin_analyzer_rule_engine_detects_generated_network_pressure() -> None:
    analyzer = AdminAnalyzer()
    admin_config = _admin_config_for_generated_rule(
        rule_id="host_runtime_thresholds",
        runbook_id="inspect_host_runtime",
        action_id="inspect_host_runtime",
        command="ss -s || true",
        thresholds=[
            {
                "metric": "host_tcp_syn_recv",
                "op": "gte",
                "value": 200,
                "incident_type": "network.syn_flood_suspected",
                "urgency": "critical",
                "risk_level": "high",
            }
        ],
    )
    snapshot = {
        "servers": [
            {
                "server_id": "scan:host:runtime",
                "metrics": {
                    "host_tcp_syn_recv": 250,
                    "host_tcp_established": 20,
                },
                "returncode": 0,
                "timed_out": False,
            },
        ],
    }

    result = analyzer.analyze(snapshot=snapshot, llm_output="", admin_config=admin_config)

    Draft202012Validator(AdminAnalyzerDecisionSchema).validate(result)
    assert result["action"] == "notify_admin"
    assert result["diagnosis"] == "network.syn_flood_suspected"
    assert result["incident_type"] == "network.syn_flood_suspected"
    assert result["urgency"] == "critical"
    assert result["secondary_cli_command"] == "ss -s || true"


def test_admin_analyzer_rule_engine_detects_generated_systemd_restart_loop() -> None:
    analyzer = AdminAnalyzer()
    admin_config = _admin_config_for_generated_rule(
        rule_id="systemd_worker_down",
        runbook_id="inspect_systemd_worker",
        action_id="inspect_systemd_worker",
        command="systemctl status worker --no-pager || journalctl -u worker -n 120 --no-pager",
        thresholds=[
            {
                "metric": "systemd_worker_restart_count",
                "op": "gte",
                "value": 3,
                "incident_type": "runtime.service_restart_loop",
            }
        ],
    )
    snapshot = {
        "servers": [
            {
                "server_id": "scan:systemd:worker",
                "metrics": {
                    "systemd_worker_state": "running",
                    "systemd_worker_restart_count": 7,
                },
                "returncode": 0,
                "timed_out": False,
            },
        ],
    }

    result = analyzer.analyze(snapshot=snapshot, llm_output="", admin_config=admin_config)

    Draft202012Validator(AdminAnalyzerDecisionSchema).validate(result)
    assert result["action"] == "notify_admin"
    assert result["diagnosis"] == "runtime.service_restart_loop"
    assert result["suggested_runbook_ids"] == ["inspect_systemd_worker"]
    assert "journalctl -u worker" in result["secondary_cli_command"]


def test_admin_analyzer_rule_engine_cpu_high_without_root_cause_notifies_only() -> None:
    analyzer = AdminAnalyzer()
    snapshot = {
        "servers": [
            {"server_id": "srv-1", "metrics": {"cpu_usage_pct": 98}},
        ]
    }

    result = analyzer.analyze(snapshot=snapshot, llm_output="")
    Draft202012Validator(AdminAnalyzerDecisionSchema).validate(result)
    assert result["action"] == "notify_admin"
    assert result["diagnosis"] == "cpu_high_without_root_cause"
    assert result["confidence"] == "low"


def test_admin_analyzer_rule_engine_detects_ssl_expiry_warning_and_critical() -> None:
    analyzer = AdminAnalyzer()
    warning_result = analyzer.analyze(
        snapshot={"servers": [{"server_id": "srv-w", "metrics": {"ssl_days_left": 10}}]},
        llm_output="",
    )
    critical_result = analyzer.analyze(
        snapshot={"servers": [{"server_id": "srv-c", "metrics": {"ssl_days_left": 2}}]},
        llm_output="",
    )

    Draft202012Validator(AdminAnalyzerDecisionSchema).validate(warning_result)
    Draft202012Validator(AdminAnalyzerDecisionSchema).validate(critical_result)
    assert warning_result["action"] == "notify_admin"
    assert warning_result["diagnosis"] == "ssl_expiring_warning"
    assert warning_result["urgency"] == "warning"
    assert critical_result["action"] == "notify_admin"
    assert critical_result["diagnosis"] == "ssl_expiring_critical"
    assert critical_result["urgency"] == "critical"


def test_admin_analyzer_removed_service_rules_delegate_to_llm_parser(monkeypatch) -> None:
    analyzer = AdminAnalyzer()
    snapshot = {
        "servers": [
            {"server_id": "web-1", "metrics": {"http_status": 502}},
            {"server_id": "php-1", "metrics": {"service_php_fpm": False}},
        ]
    }
    called = {"value": False}

    def _fake_parse(*_args, **_kwargs):
        called["value"] = True
        return {
            "diagnosis": "delegated_to_llm",
            "confidence": "medium",
            "action": "clear_logs",
            "reason": "llm_parser_called",
            "urgency": "warning",
        }

    monkeypatch.setattr(_MODULE, "parse_normalize_validate", _fake_parse)

    result = analyzer.analyze(snapshot=snapshot, llm_output='{"invalid":"payload"}')
    Draft202012Validator(AdminAnalyzerDecisionSchema).validate(result)
    assert result["action"] == "clear_logs"
    assert result["reason"] == "llm_parser_called"
    assert called["value"] is True


def test_admin_analyzer_cli_post_step_refines_low_confidence_notify_admin() -> None:
    analyzer = AdminAnalyzer()
    snapshot = {
        "servers": [
            {"server_id": "srv-1", "metrics": {"cpu_usage_pct": 98}},
        ]
    }
    cli_output = (
        '{"diagnosis":"tmp_growth",'
        '"confidence":"medium",'
        '"action":"clear_tmp",'
        '"reason":"cli_detected_tmp_pressure",'
        '"urgency":"warning"}'
    )

    result = analyzer.analyze(snapshot=snapshot, llm_output="", cli_output=cli_output)
    Draft202012Validator(AdminAnalyzerDecisionSchema).validate(result)
    assert result["action"] == "clear_tmp"
    assert str(result["reason"]).startswith("cli_post_analysis:")


def test_admin_analyzer_cli_post_step_does_not_override_non_low_primary_decision() -> None:
    analyzer = AdminAnalyzer()
    snapshot = {
        "servers": [
            {"server_id": "srv-1", "metrics": {"disk_usage_pct": 95}},
        ]
    }
    cli_output = (
        '{"diagnosis":"noise",'
        '"confidence":"low",'
        '"action":"notify_admin",'
        '"reason":"cli_noise",'
        '"urgency":"warning"}'
    )

    result = analyzer.analyze(snapshot=snapshot, llm_output="", cli_output=cli_output)
    Draft202012Validator(AdminAnalyzerDecisionSchema).validate(result)
    assert result["action"] == "clear_logs"
    assert result["reason"] == "rule_engine:disk_usage_threshold_exceeded"


def test_admin_analyzer_cli_post_step_ignores_invalid_cli_output() -> None:
    analyzer = AdminAnalyzer()
    snapshot = {
        "servers": [
            {"server_id": "srv-1", "metrics": {"cpu_usage_pct": 99}},
        ]
    }

    result = analyzer.analyze(snapshot=snapshot, llm_output="", cli_output="not-json")
    Draft202012Validator(AdminAnalyzerDecisionSchema).validate(result)
    assert result["action"] == "notify_admin"
    assert result["diagnosis"] == "cpu_high_without_root_cause"


def test_admin_analyzer_cli_feedback_envelope_finalizes_low_confidence_notify_admin() -> None:
    analyzer = AdminAnalyzer()
    snapshot = {
        "servers": [
            {"server_id": "srv-1", "metrics": {"cpu_usage_pct": 99}},
        ]
    }
    cli_output = (
        '{"secondary_cli_feedback":{'
        '"command":"top -b -n 1 | head -n 30",'
        '"transport":"local",'
        '"stdout":"top snapshot\\n",'
        '"stderr":"",'
        '"returncode":0,'
        '"timed_out":false'
        "}}"
    )

    result = analyzer.analyze(snapshot=snapshot, llm_output="", cli_output=cli_output)

    Draft202012Validator(AdminAnalyzerDecisionSchema).validate(result)
    assert result["action"] == "notify_admin"
    assert result["confidence"] == "medium"
    assert result["reason"] == "cli_post_analysis:diagnostic_output_captured"
    assert result["incident_type"] == "saturation.cpu_high"
    assert result["risk_level"] == "medium"
    assert "secondary_cli_command" not in result


def test_admin_analyzer_isolated_between_sequential_intents() -> None:
    analyzer = AdminAnalyzer()

    first = analyzer.analyze(
        snapshot={"servers": [{"server_id": "web-1", "metrics": {"http_status": 502, "php_fpm_state": "down"}}]},
        llm_output=(
            '{"diagnosis":"llm_first","confidence":"low","action":"notify_admin","reason":"insufficient_data_first","urgency":"warning"}'
        ),
    )
    second = analyzer.analyze(
        snapshot={"servers": [{"server_id": "srv-2", "metrics": {"disk_usage_pct": 95}}]},
        llm_output=(
            '{"diagnosis":"llm_second","confidence":"medium","action":"restart_postgresql","reason":"second_llm","urgency":"critical"}'
        ),
    )

    Draft202012Validator(AdminAnalyzerDecisionSchema).validate(first)
    Draft202012Validator(AdminAnalyzerDecisionSchema).validate(second)
    assert first["action"] == "notify_admin"
    assert first["reason"] == "insufficient_data_first"
    assert "secondary_cli_command" in first
    assert second["action"] == "clear_logs"
    assert second["reason"] == "rule_engine:disk_usage_threshold_exceeded"
    assert "secondary_cli_command" not in second
    assert first["diagnosis"] != second["diagnosis"]


def test_admin_analyzer_loads_prompts_yaml_and_builds_fallback_prompt(tmp_path: Path) -> None:
    prompts_path = tmp_path / "prompts.yaml"
    prompts_path.write_text(
        (
            "prompts:\n"
            "  llm_fallback_system: |\n"
            "    CUSTOM_SYSTEM_PROMPT\n"
            "  llm_json_contract: |\n"
            "    CUSTOM_JSON_CONTRACT\n"
            "  llm_snapshot_prefix: |\n"
            "    CUSTOM_SNAPSHOT_PREFIX\n"
        ),
        encoding="utf-8",
    )
    analyzer = AdminAnalyzer(prompts_path=str(prompts_path))

    prompt = analyzer.build_llm_fallback_prompt(
        snapshot={"servers": [{"server_id": "srv-1", "metrics": {"http_status": 500}}]}
    )

    assert "CUSTOM_SYSTEM_PROMPT" in prompt
    assert "CUSTOM_JSON_CONTRACT" in prompt
    assert "CUSTOM_SNAPSHOT_PREFIX" in prompt
    assert '"server_id": "srv-1"' in prompt


def test_admin_analyzer_analyze_uses_loaded_prompt_for_llm_path(tmp_path: Path) -> None:
    prompts_path = tmp_path / "prompts.yaml"
    prompts_path.write_text(
        (
            "prompts:\n"
            "  llm_fallback_system: |\n"
            "    PROMPT_MARKER\n"
        ),
        encoding="utf-8",
    )
    analyzer = AdminAnalyzer(prompts_path=str(prompts_path))
    llm_raw = (
        '{"diagnosis":"normal",'
        '"confidence":"medium",'
        '"action":"notify_admin",'
        '"reason":"fallback path parsed",'
        '"urgency":"info"}'
    )

    result = analyzer.analyze(
        snapshot={"servers": [{"server_id": "srv-2", "metrics": {"cpu_usage": 0.2}}]},
        llm_output=llm_raw,
    )

    Draft202012Validator(AdminAnalyzerDecisionSchema).validate(result)
    assert result["diagnosis"] == "normal"
    assert "PROMPT_MARKER" in analyzer.last_llm_prompt


def test_admin_analyzer_loads_real_prompts_file() -> None:
    # Do not mock the prompts_path to ensure the real modes/admin/prompts.yaml is loaded
    analyzer = AdminAnalyzer()

    prompt = analyzer.build_llm_fallback_prompt(
        snapshot={"servers": [{"server_id": "test", "metrics": {}}]}
    )

    assert "LLM Fallback Analyzer" in prompt
    assert "high|medium|low" in prompt
    assert "session_scoped_remediation_action_id" in prompt
    assert "secondary_cli_command" in prompt
    assert "Snapshot JSON" in prompt


def test_admin_analyzer_real_prompt_explains_secondary_cli_policy() -> None:
    analyzer = AdminAnalyzer()

    prompt = analyzer.build_llm_fallback_prompt(
        snapshot={"servers": [{"server_id": "test", "metrics": {}}]}
    )

    assert "confidence=low" in prompt
    assert "сигналов недостаточно" in prompt
    assert "action=notify_admin" in prompt
    assert "pinned_cli" in prompt
    assert "не добавляй secondary_cli_command" in prompt
