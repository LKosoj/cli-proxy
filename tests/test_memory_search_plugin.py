import asyncio

from agent.plugins.memory_search import MemorySearchTool


def test_memory_search_plugin_returns_relevant_snippets(tmp_path):
    async def _run():
        (tmp_path / "MEMORY.md").write_text(
            "- 2026-02-10 12:00: [CONFIG] [LAYER:semantic] [SRC:agent] [ID:abc123abc123] sqlite fts5 включен\n",
            encoding="utf-8",
        )
        (tmp_path / "SESSION.json").write_text('{"orchestrator_by_task": {}}', encoding="utf-8")
        tool = MemorySearchTool()
        ctx = {"cwd": str(tmp_path), "state_root": str(tmp_path)}
        resp = await tool.execute({"query": "sqlite fts5", "limit": 5}, ctx)
        assert resp.get("success") is True
        assert "sqlite" in str(resp.get("output") or "").lower()
        assert isinstance(resp.get("items"), list)
        assert resp.get("items")

    asyncio.run(_run())
