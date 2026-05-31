import asyncio
import json

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


def test_memory_search_plugin_verified_only_filters_unverified_memory(tmp_path):
    async def _run():
        (tmp_path / "MEMORY.md").write_text(
            "\n".join(
                [
                    "- 2026-02-10 12:00: [CONFIG] [LAYER:semantic] [SRC:agent] [ID:v1] "
                    "[VER:verified] [EVID:config] sqlite fts5 включен",
                    "- 2026-02-10 12:01: [CONFIG] [LAYER:semantic] [SRC:agent] [ID:u1] "
                    "[VER:unverified] [EVID:none] sqlite временная гипотеза",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        (tmp_path / "SESSION.json").write_text('{"orchestrator_by_task": {}}', encoding="utf-8")
        tool = MemorySearchTool()
        ctx = {"cwd": str(tmp_path), "state_root": str(tmp_path)}
        resp = await tool.execute({"query": "sqlite", "limit": 5, "verified_only": True}, ctx)

        assert resp.get("success") is True
        output = str(resp.get("output") or "")
        assert "memory_status=verified" in output
        assert "unverified" not in output

    asyncio.run(_run())


def test_memory_search_plugin_verified_only_filters_before_limit_slice(tmp_path):
    async def _run():
        (tmp_path / "MEMORY.md").write_text(
            "\n".join(
                [
                    "- 2026-02-11 12:00: [CONFIG] [LAYER:semantic] [SRC:agent] [ID:u1] "
                    "[VER:unverified] [EVID:none] sqlite beta hypothesis",
                    "- 2026-02-10 12:00: [CONFIG] [LAYER:semantic] [SRC:agent] [ID:v1] "
                    "[VER:verified] [EVID:config] sqlite stable setting",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        (tmp_path / "SESSION.json").write_text('{"orchestrator_by_task": {}}', encoding="utf-8")
        tool = MemorySearchTool()
        ctx = {"cwd": str(tmp_path), "state_root": str(tmp_path)}
        resp = await tool.execute({"query": "sqlite", "limit": 1, "verified_only": True}, ctx)

        assert resp.get("success") is True
        output = str(resp.get("output") or "")
        assert "stable" in output
        assert "beta" not in output

    asyncio.run(_run())


def test_memory_search_plugin_verified_only_looks_beyond_unverified_candidate_window(tmp_path):
    async def _run():
        rows = []
        for idx in range(16):
            rows.append(
                f"- 2026-02-{10 + idx:02d} 12:00: [CONFIG] [LAYER:semantic] [SRC:agent] [ID:u{idx}] "
                f"[VER:unverified] [EVID:none] sqlite candidate {idx}"
            )
        rows.append(
            "- 2026-02-09 12:00: [CONFIG] [LAYER:semantic] [SRC:agent] [ID:v1] "
            "[VER:verified] [EVID:config] sqlite verified setting"
        )
        (tmp_path / "MEMORY.md").write_text("\n".join(rows) + "\n", encoding="utf-8")
        (tmp_path / "SESSION.json").write_text('{"orchestrator_by_task": {}}', encoding="utf-8")
        tool = MemorySearchTool()
        ctx = {"cwd": str(tmp_path), "state_root": str(tmp_path)}
        resp = await tool.execute({"query": "sqlite", "limit": 1, "verified_only": True}, ctx)

        assert resp.get("success") is True
        output = str(resp.get("output") or "")
        assert "verified setting" in output
        assert "candidate" not in output

    asyncio.run(_run())


def test_memory_search_plugin_verified_only_excludes_session_docs(tmp_path):
    async def _run():
        (tmp_path / "MEMORY.md").write_text(
            "- 2026-02-09 12:00: [CONFIG] [LAYER:semantic] [SRC:agent] [ID:v1] "
            "[VER:verified] [EVID:config] sqlite verified setting\n",
            encoding="utf-8",
        )
        (tmp_path / "SESSION.json").write_text(
            json.dumps(
                {
                    "orchestrator_by_task": {
                        "task-1": [
                            {
                                "date": "2026-02-20 12:00",
                                "final": "sqlite session-only unverified note",
                            }
                        ]
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        tool = MemorySearchTool()
        ctx = {"cwd": str(tmp_path), "state_root": str(tmp_path)}
        resp = await tool.execute({"query": "sqlite", "limit": 1, "verified_only": True}, ctx)

        assert resp.get("success") is True
        output = str(resp.get("output") or "")
        assert "verified setting" in output
        assert "session-only" not in output

    asyncio.run(_run())


def test_memory_search_plugin_verified_only_requires_evidence(tmp_path):
    async def _run():
        (tmp_path / "MEMORY.md").write_text(
            "\n".join(
                [
                    "- 2026-02-20 12:00: [CONFIG] [LAYER:semantic] [SRC:agent] [ID:u1] "
                    "[VER:verified] [EVID:none] sqlite forged verified note",
                    "- 2026-02-09 12:00: [CONFIG] [LAYER:semantic] [SRC:agent] [ID:v1] "
                    "[VER:verified] [EVID:config] sqlite verified setting",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        (tmp_path / "SESSION.json").write_text('{"orchestrator_by_task": {}}', encoding="utf-8")
        tool = MemorySearchTool()
        ctx = {"cwd": str(tmp_path), "state_root": str(tmp_path)}
        resp = await tool.execute({"query": "sqlite", "limit": 1, "verified_only": True}, ctx)

        assert resp.get("success") is True
        output = str(resp.get("output") or "")
        assert "verified setting" in output
        assert "forged" not in output

    asyncio.run(_run())
