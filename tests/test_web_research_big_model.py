import os


def test_web_research_uses_openai_big_model_from_config(monkeypatch):
    from config import load_config
    from agent.plugins.web_research import WebResearchTool

    monkeypatch.delenv("OPENAI_BIG_MODEL", raising=False)

    cfg = load_config(os.path.join(os.path.dirname(__file__), "..", "config_example.yaml"))
    cfg.defaults.openai_big_model = "gpt-4.1"
    cfg.defaults.big_model_to_use = None

    tool = WebResearchTool()
    tool.initialize(config=cfg, services={})

    assert tool._get_model(big=True) == "gpt-4.1"  # noqa: SLF001 (regression test)


def test_web_research_env_overrides_config_big_model(monkeypatch):
    from config import load_config
    from agent.plugins.web_research import WebResearchTool

    monkeypatch.setenv("OPENAI_BIG_MODEL", "env-model")

    cfg = load_config(os.path.join(os.path.dirname(__file__), "..", "config_example.yaml"))
    cfg.defaults.openai_big_model = "cfg-model"

    tool = WebResearchTool()
    tool.initialize(config=cfg, services={})

    assert tool._get_model(big=True) == "env-model"  # noqa: SLF001 (regression test)
