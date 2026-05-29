import os

from app.services.sandbox_service import AgentSandboxService
from utils.paths import legacy_sandbox_session_dir, sandbox_session_dir


def test_agent_sandbox_service_isolates_scoped_session_paths(tmp_path):
    service = AgentSandboxService(str(tmp_path))
    service.configure()

    first_dir = sandbox_session_dir(str(tmp_path), "1_s1")
    second_dir = sandbox_session_dir(str(tmp_path), "2_s1")
    os.makedirs(first_dir, exist_ok=True)
    os.makedirs(second_dir, exist_ok=True)

    assert service.clear_session("1_s1") is True
    assert not os.path.exists(first_dir)
    assert os.path.exists(second_dir)


def test_agent_sandbox_service_refuses_legacy_raw_session_token(tmp_path):
    service = AgentSandboxService(str(tmp_path))
    service.configure()

    legacy_dir = legacy_sandbox_session_dir(str(tmp_path), "s1")
    os.makedirs(legacy_dir, exist_ok=True)

    assert service.clear_session("s1") is False
    assert os.path.exists(legacy_dir)
