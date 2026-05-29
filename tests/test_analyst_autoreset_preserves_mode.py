from modes.analyst.template_service import get_template_for_session


class _FakeAnalyst:
    def __init__(self) -> None:
        self.clear_calls = 0
        self.prompts: list[str] = []

    def clear_session_cache(self, _session_id: str) -> None:
        self.clear_calls += 1

    async def run(self, _session, _analyst_prompt: str, _bot_app, _context, _dest):
        self.prompts.append(str(_analyst_prompt or ""))
        return "ok"

    def get_template_for_session(self, session):
        return get_template_for_session(session)


class _FakeManager:
    def __init__(self) -> None:
        self.persist_calls = 0

    def _persist_sessions(self) -> None:
        self.persist_calls += 1


class _FakeSessionControl:
    def __init__(self, manager: _FakeManager) -> None:
        self._manager = manager
        self.persist_calls = 0

    def persist(self) -> None:
        self.persist_calls += 1
        self._manager._persist_sessions()
