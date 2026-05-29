import pytest
import yaml

from modes.admin.baseline import apply_scan_result
from modes.admin.facade import AdminAutonomyService
from modes.admin.prereqs import (
    PREREQS_CHECK_ID,
    PREREQS_RECOMMENDED,
    PREREQS_REQUIRED,
    evaluate_prereqs,
    generate_bootstrap_script,
    parse_prereqs_output,
    prereqs_command,
)
from modes.admin.snapshot_store import admin_root


# --- manifest ---------------------------------------------------------------

def test_manifest_required_has_bash_awk_ss():
    names = {t.name for t in PREREQS_REQUIRED}
    assert {"bash", "awk", "ss"}.issubset(names)


def test_manifest_each_tool_has_pkg_per_pm():
    for t in (*PREREQS_REQUIRED, *PREREQS_RECOMMENDED):
        assert t.apt, f"{t.name}: missing apt pkg"
        assert t.dnf, f"{t.name}: missing dnf pkg"
        assert t.apk, f"{t.name}: missing apk pkg"
        assert t.pacman, f"{t.name}: missing pacman pkg"


# --- command / parser -------------------------------------------------------

def test_prereqs_command_covers_all_tools():
    cmd = prereqs_command()
    for t in (*PREREQS_REQUIRED, *PREREQS_RECOMMENDED):
        assert f'"{t.name}"' in cmd


def test_prereqs_command_with_subset():
    cmd = prereqs_command(["bash", "jq"])
    assert 'command -v "bash"' in cmd
    assert 'command -v "jq"' in cmd
    assert "awk" not in cmd


def test_prereqs_command_sanitizes_dangerous_chars():
    cmd = prereqs_command(['bash"; rm -rf /'])
    assert '"; rm -rf /' not in cmd
    assert "bash;rm-rf/" not in cmd or "rm" not in cmd.split("echo", 1)[0]


def test_prereqs_command_empty_returns_true():
    assert prereqs_command([]) == "true"


def test_parse_prereqs_output_basic():
    text = "bash ok\nawk missing\njq ok\n"
    parsed = parse_prereqs_output(text)
    assert parsed == {"bash": True, "awk": False, "jq": True}


def test_parse_prereqs_output_ignores_garbage():
    text = "bash ok\nnoise noise noise\nawk MISSING\nlsof ok\n"
    parsed = parse_prereqs_output(text)
    # строка из 3 слов выбрасывается; MISSING в uppercase — нормализуется
    assert parsed == {"bash": True, "awk": False, "lsof": True}


# --- evaluate ---------------------------------------------------------------

def test_evaluate_all_present_is_ok():
    presence = {t.name: True for t in (*PREREQS_REQUIRED, *PREREQS_RECOMMENDED)}
    report = evaluate_prereqs(presence, distro_id="ubuntu")
    assert report.ok is True
    assert report.required_missing == []
    assert report.installable == []
    assert report.pkg_manager == "apt"


def test_evaluate_required_missing_blocks_ok():
    presence = {t.name: True for t in (*PREREQS_REQUIRED, *PREREQS_RECOMMENDED)}
    presence["bash"] = False
    report = evaluate_prereqs(presence, distro_id="ubuntu")
    assert report.ok is False
    assert "bash" in report.required_missing
    assert "bash" in report.installable


def test_evaluate_id_like_fallback():
    # CentOS Stream иногда ID=centos ID_LIKE="rhel fedora"; rocky → dnf
    report = evaluate_prereqs({}, distro_id="unknowndistro", id_like="debian ubuntu")
    assert report.pkg_manager == "apt"


def test_evaluate_unknown_distro_yields_no_installable():
    report = evaluate_prereqs({t.name: False for t in PREREQS_REQUIRED}, distro_id="haikuos")
    assert report.pkg_manager is None
    assert report.installable == []
    assert report.required_missing  # но сами tool'ы зарегистрированы


# --- bootstrap script ------------------------------------------------------

def test_generate_bootstrap_apt():
    presence = {t.name: True for t in (*PREREQS_REQUIRED, *PREREQS_RECOMMENDED)}
    presence["shellcheck"] = False
    report = evaluate_prereqs(presence, distro_id="debian")
    script = generate_bootstrap_script(report)
    assert "#!/bin/bash" in script
    assert "set -euo pipefail" in script
    assert "apt-get update" in script
    assert "apt-get install -y --no-install-recommends shellcheck" in script


def test_generate_bootstrap_dnf():
    presence = {t.name: True for t in (*PREREQS_REQUIRED, *PREREQS_RECOMMENDED)}
    presence["shellcheck"] = False
    report = evaluate_prereqs(presence, distro_id="rocky")
    script = generate_bootstrap_script(report)
    # dnf shellcheck пакет в rhel-family называется ShellCheck
    assert "dnf install -y ShellCheck" in script


def test_generate_bootstrap_apk():
    presence = {t.name: True for t in (*PREREQS_REQUIRED, *PREREQS_RECOMMENDED)}
    presence["jq"] = False
    report = evaluate_prereqs(presence, distro_id="alpine")
    script = generate_bootstrap_script(report)
    assert "apk add --no-cache jq" in script


def test_generate_bootstrap_pacman():
    presence = {t.name: True for t in (*PREREQS_REQUIRED, *PREREQS_RECOMMENDED)}
    presence["curl"] = False
    report = evaluate_prereqs(presence, distro_id="arch")
    script = generate_bootstrap_script(report)
    assert "pacman -Sy --noconfirm --needed curl" in script


def test_generate_bootstrap_nothing_to_install():
    presence = {t.name: True for t in (*PREREQS_REQUIRED, *PREREQS_RECOMMENDED)}
    report = evaluate_prereqs(presence, distro_id="ubuntu")
    script = generate_bootstrap_script(report)
    assert "nothing to install" in script


def test_generate_bootstrap_unknown_pm_emits_fallback():
    presence = {t.name: False for t in PREREQS_REQUIRED}
    report = evaluate_prereqs(presence, distro_id="haikuos")
    script = generate_bootstrap_script(report)
    # нет package manager → идёт по ветке "unsupported"
    assert "unsupported" in script or "install manually" in script


# --- facade integration -----------------------------------------------------

def _write_config(workdir, servers):
    root = admin_root(str(workdir))
    root.mkdir(parents=True, exist_ok=True)
    payload = {
        "admin": {
            "allowlist": {"local": {}, "ssh": {}},
            "monitor": {"servers": servers},
            "runtime": {},
        }
    }
    (root / "config.yaml").write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def _baseline_with_prereqs(workdir, sid, presence, distro_id="ubuntu"):
    apply_scan_result(
        str(workdir),
        sid,
        {
            "server_id": sid,
            "checks": {
                "os.os_release": {"ID": distro_id, "ID_LIKE": "debian"},
                PREREQS_CHECK_ID: dict(presence),
            },
        },
    )


def test_facade_check_server_prereqs_reads_baseline(tmp_path):
    _write_config(tmp_path, [{"server_id": "web-01"}])
    presence = {t.name: True for t in (*PREREQS_REQUIRED, *PREREQS_RECOMMENDED)}
    presence["jq"] = False
    _baseline_with_prereqs(tmp_path, "web-01", presence, distro_id="ubuntu")

    fac = AdminAutonomyService(str(tmp_path))
    report = fac.check_server_prereqs("web-01")
    assert report.ok is True  # только recommended отсутствует
    assert "jq" in report.recommended_missing
    assert report.pkg_manager == "apt"
    assert "jq" in report.installable


def test_facade_check_server_prereqs_missing_baseline_returns_all_missing(tmp_path):
    _write_config(tmp_path, [{"server_id": "web-01"}])
    fac = AdminAutonomyService(str(tmp_path))
    report = fac.check_server_prereqs("web-01")
    # Baseline нет → presence пустая → всё считается missing.
    assert report.ok is False
    assert {"bash", "awk", "ss"}.issubset(set(report.required_missing))


def test_facade_generate_bootstrap_runbook_creates_manual_runbook(tmp_path):
    _write_config(tmp_path, [{"server_id": "web-01"}])
    presence = {t.name: True for t in (*PREREQS_REQUIRED, *PREREQS_RECOMMENDED)}
    presence["lsof"] = False
    _baseline_with_prereqs(tmp_path, "web-01", presence, distro_id="ubuntu")

    fac = AdminAutonomyService(str(tmp_path))
    result = fac.generate_bootstrap_runbook("web-01")
    assert result["reason"] == "bootstrap_ready"
    assert result["runbook"]["id"] == "rb-admin-bootstrap-web-01"
    assert "bootstrap" in result["runbook"]["tags"]

    # Проверим, что runbook действительно записан и имеет confidence=0.0.
    rb = fac.get_runbook("rb-admin-bootstrap-web-01")
    assert rb is not None
    assert rb.metadata.get("auto_action", {}).get("confidence") == 0.0


def test_facade_generate_bootstrap_runbook_no_missing(tmp_path):
    _write_config(tmp_path, [{"server_id": "web-01"}])
    presence = {t.name: True for t in (*PREREQS_REQUIRED, *PREREQS_RECOMMENDED)}
    _baseline_with_prereqs(tmp_path, "web-01", presence, distro_id="ubuntu")

    fac = AdminAutonomyService(str(tmp_path))
    result = fac.generate_bootstrap_runbook("web-01")
    assert result["reason"] == "no_missing_prereqs"
    assert result["runbook"] is None


def test_facade_generate_bootstrap_runbook_writes_prereqs_audit_note(tmp_path):
    _write_config(tmp_path, [{"server_id": "web-01"}])
    presence = {t.name: True for t in (*PREREQS_REQUIRED, *PREREQS_RECOMMENDED)}
    presence["shellcheck"] = False
    _baseline_with_prereqs(tmp_path, "web-01", presence, distro_id="ubuntu")

    fac = AdminAutonomyService(str(tmp_path))
    fac.generate_bootstrap_runbook("web-01")

    mem = fac.get_memory("web-01")
    notes_text = str(mem["notes_text"] or "")
    assert "PREREQS-BOOTSTRAP" in notes_text
    assert "rb=rb-admin-bootstrap-web-01" in notes_text
    assert "distro=ubuntu" in notes_text
    assert "pm=apt" in notes_text
    # shellcheck попадает в missing_recommended и installable
    assert "missing_recommended=['shellcheck']" in notes_text
    assert "pkgs=['shellcheck']" in notes_text


def test_facade_generate_bootstrap_runbook_force_rebuild(tmp_path):
    _write_config(tmp_path, [{"server_id": "web-01"}])
    presence = {t.name: True for t in (*PREREQS_REQUIRED, *PREREQS_RECOMMENDED)}
    presence["jq"] = False
    _baseline_with_prereqs(tmp_path, "web-01", presence, distro_id="ubuntu")

    fac = AdminAutonomyService(str(tmp_path))
    fac.generate_bootstrap_runbook("web-01")
    # без force второй запуск должен упасть (runbook уже есть)
    with pytest.raises(Exception):
        fac.generate_bootstrap_runbook("web-01")
    # с force — нормально
    result = fac.generate_bootstrap_runbook("web-01", force=True)
    assert result["reason"] == "bootstrap_ready"
