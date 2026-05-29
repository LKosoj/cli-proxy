import os

from config import load_config
from modes.sdk.runtime.dispatcher import Dispatcher
from modes.sdk.runtime.profiles import build_analyst_profile
from modes.sdk.runtime.tooling.registry import ToolRegistry


def test_build_analyst_profile_is_readonly():
    cfg = load_config(os.path.join(os.path.dirname(__file__), "..", "config_example.yaml"))
    reg = ToolRegistry(cfg)
    profile = build_analyst_profile(cfg, reg)

    have = set(reg.list_tool_names())
    assert set(profile.allowed_tools).issubset(have)
    assert "use_cli" in profile.allowed_tools

    forbidden = {"write_file", "edit_file", "delete_file", "run_command"}
    for name in forbidden:
        assert name not in profile.allowed_tools


def test_dispatcher_keeps_use_cli_available_for_analyst_use_cli_steps():
    cfg = load_config(os.path.join(os.path.dirname(__file__), "..", "config_example.yaml"))
    reg = ToolRegistry(cfg)
    dispatcher = Dispatcher(cfg, reg)

    step = type("Step", (), {"id": "use_cli_repo_grounding", "step_type": "use_cli"})()
    session = type("Session", (), {"executor_profile": "analyst"})()

    profile = dispatcher.get_profile(step, session)

    assert profile.name == "analyst"
    assert "use_cli" in profile.allowed_tools
