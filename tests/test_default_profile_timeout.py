import os


def test_default_profile_timeout_is_240s():
    from config import load_config
    from agent.tooling.registry import ToolRegistry
    from agent.profiles import build_default_profile

    cfg = load_config(os.path.join(os.path.dirname(__file__), "..", "config_example.yaml"))
    reg = ToolRegistry(cfg)
    profile = build_default_profile(cfg, reg)
    assert profile.timeout_ms == 240_000

