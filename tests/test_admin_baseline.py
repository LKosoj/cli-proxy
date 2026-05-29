import asyncio
from pathlib import Path

import pytest
import yaml

from modes.admin.baseline import (
    AdminBaselineScanner,
    BaselineCheck,
    BaselineError,
    ServerSpec,
    accept_proposed_baseline,
    apply_scan_result,
    baseline_path,
    discard_proposed_baseline,
    load_baseline,
    load_proposed_baseline,
    prev_baseline_path,
    proposed_baseline_path,
)


class _FakeResult:
    def __init__(self, stdout: str = "", *, timed_out: bool = False):
        self.stdout = stdout
        self.stderr = ""
        self.returncode = 0
        self.timed_out = timed_out
        self.duration_ms = 1


class _FakeLocalTransport:
    def __init__(self, responses):
        self.responses = dict(responses)
        self.calls = []

    async def run(self, spec):
        self.calls.append(spec)
        key = spec.action_id.split(":", 1)[-1]
        payload = self.responses.get(key, "")
        if isinstance(payload, Exception):
            raise payload
        if isinstance(payload, dict):
            return _FakeResult(stdout=payload.get("stdout", ""), timed_out=payload.get("timed_out", False))
        return _FakeResult(stdout=str(payload))


def _scanner_with_fake_transport(responses) -> AdminBaselineScanner:
    transport = _FakeLocalTransport(responses)
    checks = [
        BaselineCheck(id="os.kernel", command="uname -r", parser=lambda s: s.strip()),
        BaselineCheck(id="os.hostname", command="hostname", parser=lambda s: s.strip()),
    ]
    return AdminBaselineScanner(checks=checks, local_transport=transport)


def test_scanner_runs_checks_and_returns_profile(tmp_path):
    async def _run():
        scanner = _scanner_with_fake_transport({
            "os.kernel": "6.8.0-107-generic\n",
            "os.hostname": "web-01\n",
        })
        spec = ServerSpec(server_id="web-01", transport="local", label="Web 01", tags=["web"])
        profile = await scanner.scan(spec)
        assert profile["server_id"] == "web-01"
        assert profile["transport"] == "local"
        assert profile["label"] == "Web 01"
        assert profile["tags"] == ["web"]
        assert profile["checks"]["os.kernel"] == "6.8.0-107-generic"
        assert profile["checks"]["os.hostname"] == "web-01"
        assert "errors" not in profile
    asyncio.run(_run())


def test_scanner_records_check_error_in_profile():
    async def _run():
        scanner = _scanner_with_fake_transport({
            "os.kernel": "6.8\n",
            "os.hostname": RuntimeError("ssh broke"),
        })
        profile = await scanner.scan(ServerSpec(server_id="s1", transport="local"))
        assert profile["checks"]["os.kernel"] == "6.8"
        assert profile["checks"]["os.hostname"] is None
        assert "errors" in profile
        assert "os.hostname" in profile["errors"]
    asyncio.run(_run())


def test_scanner_timed_out_becomes_error():
    async def _run():
        scanner = _scanner_with_fake_transport({
            "os.kernel": {"stdout": "", "timed_out": True},
            "os.hostname": "host",
        })
        profile = await scanner.scan(ServerSpec(server_id="s1", transport="local"))
        assert profile["checks"]["os.kernel"] is None
        assert "os.kernel" in profile.get("errors", {})
    asyncio.run(_run())


def test_scanner_records_unknown_transport_per_check_error():
    async def _run():
        scanner = _scanner_with_fake_transport({})
        profile = await scanner.scan(ServerSpec(server_id="s1", transport="carrier_pigeon"))
        assert all(profile["checks"][cid] is None for cid in profile["checks"])
        for cid, err in profile.get("errors", {}).items():
            assert "unknown transport" in err
    asyncio.run(_run())


def test_scanner_ssh_requires_host_and_key():
    async def _run():
        scanner = AdminBaselineScanner(
            checks=[BaselineCheck(id="os.kernel", command="uname -r", parser=lambda s: s.strip())],
            local_transport=_FakeLocalTransport({}),
        )
        profile = await scanner.scan(ServerSpec(server_id="s1", transport="ssh"))
        assert profile["checks"]["os.kernel"] is None
        assert "os.kernel" in profile.get("errors", {})
    asyncio.run(_run())


def test_scanner_ssh_uses_password_env(tmp_path):
    async def _run():
        ssh_dir = tmp_path / ".cli-proxy"
        ssh_dir.mkdir()
        (ssh_dir / "ssh.env").write_text("SSH_S1_PASSWORD=secret\n", encoding="utf-8")
        transport = _FakeLocalTransport({"os.kernel": "6.8\n"})
        scanner = AdminBaselineScanner(
            checks=[BaselineCheck(id="os.kernel", command="uname -r", parser=lambda s: s.strip())],
            ssh_transport=transport,
            secrets_workdir=str(tmp_path),
        )
        profile = await scanner.scan(
            ServerSpec(
                server_id="s1",
                transport="ssh",
                host="server.example",
                user="deploy",
                password_env="SSH_S1_PASSWORD",
            )
        )
        assert profile["checks"]["os.kernel"] == "6.8"
        assert transport.calls[0].password == "secret"
        assert transport.calls[0].key_path == ""
    asyncio.run(_run())


def test_apply_scan_result_first_time_creates_baseline(tmp_path):
    profile = {"server_id": "web-01", "checks": {"a": 1}}
    result = apply_scan_result(str(tmp_path), "web-01", profile)
    assert result["action"] == "created"
    assert Path(result["path"]).is_file()
    loaded = load_baseline(str(tmp_path), "web-01")
    assert loaded["checks"]["a"] == 1


def test_apply_scan_result_second_time_goes_to_proposed(tmp_path):
    apply_scan_result(str(tmp_path), "web-01", {"server_id": "web-01", "checks": {"a": 1}})
    result = apply_scan_result(str(tmp_path), "web-01", {"server_id": "web-01", "checks": {"a": 2}})
    assert result["action"] == "proposed"
    assert load_baseline(str(tmp_path), "web-01")["checks"]["a"] == 1
    assert load_proposed_baseline(str(tmp_path), "web-01")["checks"]["a"] == 2


def test_accept_proposed_baseline_moves_files(tmp_path):
    apply_scan_result(str(tmp_path), "web-01", {"server_id": "web-01", "checks": {"a": 1}})
    apply_scan_result(str(tmp_path), "web-01", {"server_id": "web-01", "checks": {"a": 2}})
    result = accept_proposed_baseline(str(tmp_path), "web-01")
    assert Path(result["accepted"]).is_file()
    assert Path(result["prev"]).is_file()
    assert load_baseline(str(tmp_path), "web-01")["checks"]["a"] == 2
    assert load_proposed_baseline(str(tmp_path), "web-01") is None
    prev_content = yaml.safe_load(Path(result["prev"]).read_text(encoding="utf-8"))
    assert prev_content["checks"]["a"] == 1


def test_accept_proposed_fails_when_no_proposed(tmp_path):
    apply_scan_result(str(tmp_path), "web-01", {"server_id": "web-01", "checks": {"a": 1}})
    with pytest.raises(BaselineError):
        accept_proposed_baseline(str(tmp_path), "web-01")


def test_discard_proposed_baseline(tmp_path):
    apply_scan_result(str(tmp_path), "web-01", {"server_id": "web-01", "checks": {"a": 1}})
    apply_scan_result(str(tmp_path), "web-01", {"server_id": "web-01", "checks": {"a": 2}})
    assert discard_proposed_baseline(str(tmp_path), "web-01") is True
    assert load_proposed_baseline(str(tmp_path), "web-01") is None
    assert discard_proposed_baseline(str(tmp_path), "web-01") is False


def test_load_baseline_missing_returns_none(tmp_path):
    assert load_baseline(str(tmp_path), "no-such") is None
    assert load_proposed_baseline(str(tmp_path), "no-such") is None


def test_load_baseline_rejects_non_mapping(tmp_path):
    path = baseline_path(str(tmp_path), "web-01")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("- a\n- b\n", encoding="utf-8")
    with pytest.raises(BaselineError):
        load_baseline(str(tmp_path), "web-01")


def test_baseline_paths_are_isolated_per_server(tmp_path):
    p1 = baseline_path(str(tmp_path), "web-01")
    p2 = baseline_path(str(tmp_path), "db-02")
    assert p1 != p2
    assert p1.parent != p2.parent
    assert proposed_baseline_path(str(tmp_path), "web-01") != proposed_baseline_path(str(tmp_path), "db-02")
    assert prev_baseline_path(str(tmp_path), "web-01") != prev_baseline_path(str(tmp_path), "db-02")
