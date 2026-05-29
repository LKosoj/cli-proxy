import pytest
import types

from app.services.advanced_orchestrator_service import AdvancedOrchestratorService


class _RegistryStub:
    def list_modes(self):
        return [
            ("agent", "Agent"),
            ("analyst", "Analyst"),
            ("manager", "Manager"),
            ("webmaster", "Webmaster"),
            ("codebase_mapper", "Codebase Mapper"),
        ]


class _SessionStub:
    def __init__(self, active_mode=None):
        self.modes = types.SimpleNamespace(active_mode=active_mode, analyst_mode="spec")
        self.orchestrator = types.SimpleNamespace(
            enabled=False,
            pending_input=None,
            last_mode_output=None,
            last_mode_id=None,
        )


def test_propose_transition_skips_agent_chain_and_picks_manager_for_plan_intent():
    svc = AdvancedOrchestratorService()
    session = _SessionStub(active_mode="analyst")

    proposal = svc.propose_transition(
        session=session,
        text="Нужно декомпозировать задачу на шаги и построить план выполнения",
        mode_registry=_RegistryStub(),
    )

    assert proposal is not None
    assert proposal.target_mode_id == "manager"


def test_propose_transition_picks_direct_cli_for_explicit_cli_intent():
    svc = AdvancedOrchestratorService()
    session = _SessionStub(active_mode="webmaster")

    proposal = svc.propose_transition(
        session=session,
        text="Без режима, только прямой CLI и одну shell команду",
        mode_registry=_RegistryStub(),
    )

    assert proposal is not None
    assert proposal.target_mode_id == "direct_cli"


def test_propose_transition_does_not_route_to_codebase_mapper() -> None:
    svc = AdvancedOrchestratorService()
    session = _SessionStub(active_mode=None)

    proposal = svc.propose_transition(
        session=session,
        text="Нужно сделать рефакторинг /infinite_bookshelf/ui/generation.py и проверь архитектуру",
        mode_registry=_RegistryStub(),
    )

    assert proposal is None or proposal.target_mode_id != "codebase_mapper"


def test_build_handoff_input_uses_previous_mode_output_if_present():
    svc = AdvancedOrchestratorService()
    session = _SessionStub(active_mode="analyst")
    session.orchestrator.last_mode_output = "FULL PREV MODE RESULT"

    text = svc.build_handoff_input(
        session=session,
        original_user_text="original user request",
    )
    assert text == "FULL PREV MODE RESULT"


def test_build_handoff_input_falls_back_to_original_user_text():
    svc = AdvancedOrchestratorService()
    session = _SessionStub(active_mode="analyst")
    session.orchestrator.last_mode_output = None

    text = svc.build_handoff_input(
        session=session,
        original_user_text="original user request",
    )
    assert text == "original user request"


def test_apply_mode_stores_previous_mode_marker():
    svc = AdvancedOrchestratorService()
    session = _SessionStub(active_mode="analyst")

    svc.apply_mode(session=session, target_mode_id="manager")

    assert session.modes.active_mode == "manager"
    assert getattr(session, "_orchestrator_prev_mode_id", "") == "analyst"


@pytest.mark.asyncio
async def test_hybrid_accepts_valid_llm_candidate():
    svc = AdvancedOrchestratorService()
    session = _SessionStub(active_mode="analyst")

    async def _llm(_config, _system, _user, response_format=None):
        return '{"mode_id":"manager","reason":"Нужна декомпозиция","confidence":0.91}'

    proposal = await svc.propose_transition_hybrid(
        session=session,
        text="сделай план и шаги",
        mode_registry=_RegistryStub(),
        app_config=object(),
        llm_router_fn=_llm,
    )
    assert proposal is not None
    assert proposal.target_mode_id == "manager"
    assert proposal.source == "llm"


@pytest.mark.asyncio
async def test_hybrid_falls_back_to_deterministic_when_llm_low_confidence():
    svc = AdvancedOrchestratorService()
    session = _SessionStub(active_mode="analyst")

    async def _llm(_config, _system, _user, response_format=None):
        return '{"mode_id":"manager","reason":"возможно","confidence":0.2}'

    proposal = await svc.propose_transition_hybrid(
        session=session,
        text="нужно декомпозировать на этапы и шаги",
        mode_registry=_RegistryStub(),
        app_config=object(),
        llm_router_fn=_llm,
    )
    assert proposal is not None
    assert proposal.target_mode_id == "manager"
    assert proposal.source == "deterministic"


@pytest.mark.asyncio
async def test_hybrid_rejects_agent_from_llm_and_falls_back():
    svc = AdvancedOrchestratorService()
    session = _SessionStub(active_mode="analyst")

    async def _llm(_config, _system, _user, response_format=None):
        return '{"mode_id":"agent","reason":"общий режим","confidence":0.99}'

    proposal = await svc.propose_transition_hybrid(
        session=session,
        text="сделай декомпозицию и план",
        mode_registry=_RegistryStub(),
        app_config=object(),
        llm_router_fn=_llm,
    )
    assert proposal is not None
    assert proposal.target_mode_id == "manager"
    assert proposal.source == "deterministic"


@pytest.mark.asyncio
async def test_hybrid_rejects_codebase_mapper_from_llm_and_falls_back():
    svc = AdvancedOrchestratorService()
    session = _SessionStub(active_mode="analyst")

    async def _llm(_config, _system, _user, response_format=None):
        return '{"mode_id":"codebase_mapper","reason":"нужен маппинг","confidence":0.99}'

    proposal = await svc.propose_transition_hybrid(
        session=session,
        text="сделай декомпозицию и план",
        mode_registry=_RegistryStub(),
        app_config=object(),
        llm_router_fn=_llm,
    )
    assert proposal is not None
    assert proposal.target_mode_id == "manager"
    assert proposal.source == "deterministic"
