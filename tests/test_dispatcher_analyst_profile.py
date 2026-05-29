import os

from config import load_config
from modes.sdk.runtime.contracts import PlanStep
from modes.sdk.runtime.dispatcher import Dispatcher
from modes.sdk.runtime.tooling.registry import ToolRegistry


def test_dispatcher_selects_analyst_profile_when_requested():
    cfg = load_config(os.path.join(os.path.dirname(__file__), "..", "config_example.yaml"))
    reg = ToolRegistry(cfg)
    dispatcher = Dispatcher(cfg, reg)

    step = PlanStep(id="s1", title="t", instruction="do")
    session = type("S", (), {"executor_profile": "analyst"})
    profile = dispatcher.get_profile(step, session)
    assert profile.name == "analyst"


def test_dispatcher_selects_analyst_profile_for_active_analyst_mode_without_explicit_executor_profile():
    cfg = load_config(os.path.join(os.path.dirname(__file__), "..", "config_example.yaml"))
    reg = ToolRegistry(cfg)
    dispatcher = Dispatcher(cfg, reg)

    step = PlanStep(id="analyze_external_reference", title="t", instruction="do")
    session = type(
        "S",
        (),
        {
            "modes": type("Modes", (), {"active_mode": "analyst"})(),
            "executor_profile": "",
            "analyst_intent_flags": {
                "document_kind": "spec",
                "requires_codebase_grounding": True,
            },
        },
    )()
    profile = dispatcher.get_profile(step, session)
    assert profile.name == "analyst"


def test_dispatcher_defaults_to_default_profile():
    cfg = load_config(os.path.join(os.path.dirname(__file__), "..", "config_example.yaml"))
    reg = ToolRegistry(cfg)
    dispatcher = Dispatcher(cfg, reg)

    step = PlanStep(id="s1", title="t", instruction="do")
    profile = dispatcher.get_profile(step, session=None)
    assert profile.name == "default"
