from modes.analyst.template_service import get_template_for_session


class _FakeAnalyst:
    def __init__(self) -> None:
        self.last_prompt = None

    async def run(self, _session, analyst_prompt: str, _bot_app, _context, _dest):
        self.last_prompt = analyst_prompt
        return "ok"

    def get_template_for_session(self, session):
        return get_template_for_session(session)


class _FakeManager:
    def __init__(self) -> None:
        self.persist_calls = 0

    def _persist_sessions(self) -> None:
        self.persist_calls += 1
