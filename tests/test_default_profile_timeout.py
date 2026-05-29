import os


def test_default_profile_timeout_is_600s():
    from config import load_config
    from modes.sdk.runtime.tooling.registry import ToolRegistry
    from modes.sdk.runtime.profiles import build_default_profile

    cfg = load_config(os.path.join(os.path.dirname(__file__), "..", "config_example.yaml"))
    reg = ToolRegistry(cfg)
    profile = build_default_profile(cfg, reg)
    assert profile.timeout_ms == 600_000
