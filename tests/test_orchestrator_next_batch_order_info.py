from config import AppConfig, DefaultsConfig, MCPConfig, TelegramConfig
from agent.contracts import PlanStep
from agent.orchestrator import OrchestratorRunner


def _make_orchestrator(tmp_path):
    cfg = AppConfig(
        telegram=TelegramConfig(token="", whitelist_chat_ids=[]),
        tools={},
        defaults=DefaultsConfig(
            workdir=str(tmp_path),
            state_path=str(tmp_path / "state.json"),
            toolhelp_path=str(tmp_path / "toolhelp.json"),
            log_path=str(tmp_path / "bot.log"),
        ),
        mcp=MCPConfig(enabled=False),
        mcp_clients=[],
        presets=[],
        path=str(tmp_path / "config.yaml"),
    )
    return OrchestratorRunner(cfg)


def test_next_batch_emits_reason_for_non_sequential_transition(tmp_path):
    orch = _make_orchestrator(tmp_path)
    steps = [
        PlanStep(id="step1", title="s1", instruction="do 1", depends_on=["prep"]),
        PlanStep(id="step2", title="s2", instruction="do 2"),
        PlanStep(id="prep", title="prep", instruction="do prep"),
    ]

    batch, diagnostics = orch._next_batch(
        steps=steps,
        completed_ok=set(),
        completed_fail=set(),
        session_id="sess1",
    )

    assert [s.id for s in batch] == ["step2"]
    assert diagnostics
    info = next((d for d in diagnostics if d.task_id.startswith("order_info:")), None)
    assert info is not None
    assert info.status == "partial"
    assert "Переход не по порядку" in info.summary
    assert "step1: ожидает зависимости (prep)" in info.summary
    assert info.tool_calls
    assert info.tool_calls[0].get("event") == "non_sequential_transition"


def test_next_batch_no_order_info_when_first_remaining_selected(tmp_path):
    orch = _make_orchestrator(tmp_path)
    steps = [
        PlanStep(id="step1", title="s1", instruction="do 1"),
        PlanStep(id="step2", title="s2", instruction="do 2"),
    ]

    batch, diagnostics = orch._next_batch(
        steps=steps,
        completed_ok=set(),
        completed_fail=set(),
        session_id="sess1",
    )

    assert [s.id for s in batch] == ["step1"]
    assert not any(d.task_id.startswith("order_info:") for d in diagnostics)
