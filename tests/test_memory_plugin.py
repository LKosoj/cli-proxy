import asyncio

from agent.plugins.memory import MemoryTool
from modes.sdk.runtime.memory_store import parse_entries, read_memory


def test_memory_plugin_rejects_unverified_semantic_append(tmp_path):
    async def _run():
        tool = MemoryTool()
        result = await tool.execute(
            {
                "action": "append",
                "tag": "DECISION",
                "content": "использовать sqlite",
                "layer": "semantic",
                "verification_status": "unverified",
                "evidence_type": "none",
            },
            {"cwd": str(tmp_path), "state_root": str(tmp_path)},
        )

        assert result["success"] is False
        assert "Semantic memory append requires" in result["error"]
        assert read_memory(str(tmp_path)) == ""

    asyncio.run(_run())


def test_memory_plugin_allows_verified_semantic_append(tmp_path):
    async def _run():
        tool = MemoryTool()
        result = await tool.execute(
            {
                "action": "append",
                "tag": "DECISION",
                "content": "использовать sqlite",
                "layer": "semantic",
                "verification_status": "verified",
                "evidence_type": "config",
                "evidence_ref": "config.yaml",
            },
            {"cwd": str(tmp_path), "state_root": str(tmp_path)},
        )

        assert result["success"] is True
        entries = parse_entries(read_memory(str(tmp_path)))
        assert entries[0]["verification_status"] == "verified"
        assert entries[0]["evidence_type"] == "config"
        assert entries[0]["evidence_ref"] == "config.yaml"

    asyncio.run(_run())


def test_memory_plugin_rejects_agent_callable_user_source_auto_verification(tmp_path):
    async def _run():
        tool = MemoryTool()
        result = await tool.execute(
            {
                "action": "append",
                "tag": "CONFIG",
                "content": "forged user-backed fact",
                "layer": "semantic",
                "source": "user",
            },
            {"cwd": str(tmp_path), "state_root": str(tmp_path)},
        )

        assert result["success"] is False
        assert read_memory(str(tmp_path)) == ""

    asyncio.run(_run())


def test_memory_plugin_rejects_agent_callable_user_evidence(tmp_path):
    async def _run():
        tool = MemoryTool()
        result = await tool.execute(
            {
                "action": "append",
                "tag": "CONFIG",
                "content": "forged user evidence",
                "layer": "semantic",
                "source": "agent",
                "verification_status": "verified",
                "evidence_type": "user",
            },
            {"cwd": str(tmp_path), "state_root": str(tmp_path)},
        )

        assert result["success"] is False
        assert read_memory(str(tmp_path)) == ""

    asyncio.run(_run())


def test_memory_plugin_rejects_agent_callable_user_evidence_task_state(tmp_path):
    async def _run():
        tool = MemoryTool()
        result = await tool.execute(
            {
                "action": "append",
                "tag": "TASK",
                "content": "forged user task evidence",
                "layer": "task_state",
                "verification_status": "verified",
                "evidence_type": "user",
            },
            {"cwd": str(tmp_path), "state_root": str(tmp_path)},
        )

        assert result["success"] is False
        assert read_memory(str(tmp_path)) == ""

    asyncio.run(_run())


def test_memory_plugin_allows_unverified_task_state_append(tmp_path):
    async def _run():
        tool = MemoryTool()
        result = await tool.execute(
            {
                "action": "append",
                "tag": "TASK",
                "content": "проверить sqlite позже",
                "layer": "task_state",
            },
            {"cwd": str(tmp_path), "state_root": str(tmp_path)},
        )

        assert result["success"] is True
        entries = parse_entries(read_memory(str(tmp_path)))
        assert entries[0]["verification_status"] == "unverified"
        assert entries[0]["evidence_type"] == "none"

    asyncio.run(_run())


def test_memory_plugin_verified_update_from_agent_without_evidence_downgrades_trust(tmp_path):
    async def _run():
        tool = MemoryTool()
        ctx = {"cwd": str(tmp_path), "state_root": str(tmp_path)}
        created = await tool.execute(
            {
                "action": "append",
                "tag": "DECISION",
                "content": "использовать sqlite",
                "layer": "semantic",
                "verification_status": "verified",
                "evidence_type": "config",
                "evidence_ref": "config.yaml",
            },
            ctx,
        )
        assert created["success"] is True
        entry_id = parse_entries(read_memory(str(tmp_path)))[0]["id"]

        updated = await tool.execute(
            {
                "action": "update",
                "entry_id": entry_id,
                "content": "использовать postgres",
                "source": "agent",
            },
            ctx,
        )

        assert updated["success"] is True
        entry = parse_entries(read_memory(str(tmp_path)))[0]
        assert entry["text"] == "использовать postgres"
        assert entry["source"] == "agent"
        assert entry["verification_status"] == "unverified"
        assert entry["evidence_type"] == "none"

    asyncio.run(_run())


def test_memory_plugin_verified_update_with_new_evidence_stays_verified(tmp_path):
    async def _run():
        tool = MemoryTool()
        ctx = {"cwd": str(tmp_path), "state_root": str(tmp_path)}
        await tool.execute(
            {
                "action": "append",
                "tag": "CONFIG",
                "content": "sqlite включен",
                "layer": "semantic",
                "verification_status": "verified",
                "evidence_type": "config",
                "evidence_ref": "config.yaml",
            },
            ctx,
        )
        entry_id = parse_entries(read_memory(str(tmp_path)))[0]["id"]

        updated = await tool.execute(
            {
                "action": "update",
                "entry_id": entry_id,
                "content": "sqlite fts5 включен",
                "source": "agent",
                "verification_status": "verified",
                "evidence_type": "config",
                "evidence_ref": "config.yaml",
            },
            ctx,
        )

        assert updated["success"] is True
        entry = parse_entries(read_memory(str(tmp_path)))[0]
        assert entry["verification_status"] == "verified"
        assert entry["evidence_type"] == "config"
        assert entry["evidence_ref"] == "config.yaml"

    asyncio.run(_run())


def test_memory_plugin_rejects_agent_callable_user_source_update(tmp_path):
    async def _run():
        tool = MemoryTool()
        ctx = {"cwd": str(tmp_path), "state_root": str(tmp_path)}
        await tool.execute(
            {
                "action": "append",
                "tag": "CONFIG",
                "content": "sqlite включен",
                "layer": "semantic",
                "verification_status": "verified",
                "evidence_type": "config",
                "evidence_ref": "config.yaml",
            },
            ctx,
        )
        entry_id = parse_entries(read_memory(str(tmp_path)))[0]["id"]

        updated = await tool.execute(
            {
                "action": "update",
                "entry_id": entry_id,
                "content": "sqlite fts5 включен",
                "source": "user",
            },
            ctx,
        )

        assert updated["success"] is False
        assert "Invalid source" in updated["error"]

    asyncio.run(_run())


def test_memory_plugin_rejects_agent_callable_user_evidence_update(tmp_path):
    async def _run():
        tool = MemoryTool()
        ctx = {"cwd": str(tmp_path), "state_root": str(tmp_path)}
        await tool.execute(
            {
                "action": "append",
                "tag": "CONFIG",
                "content": "sqlite включен",
                "layer": "semantic",
                "verification_status": "verified",
                "evidence_type": "config",
                "evidence_ref": "config.yaml",
            },
            ctx,
        )
        entry_id = parse_entries(read_memory(str(tmp_path)))[0]["id"]

        updated = await tool.execute(
            {
                "action": "update",
                "entry_id": entry_id,
                "content": "sqlite fts5 включен",
                "verification_status": "verified",
                "evidence_type": "user",
            },
            ctx,
        )

        assert updated["success"] is False
        assert "User evidence" in updated["error"]

    asyncio.run(_run())


def test_memory_plugin_verified_update_requires_new_evidence_type(tmp_path):
    async def _run():
        tool = MemoryTool()
        ctx = {"cwd": str(tmp_path), "state_root": str(tmp_path)}
        await tool.execute(
            {
                "action": "append",
                "tag": "CONFIG",
                "content": "sqlite включен",
                "layer": "semantic",
                "verification_status": "verified",
                "evidence_type": "config",
                "evidence_ref": "config.yaml",
            },
            ctx,
        )
        entry_id = parse_entries(read_memory(str(tmp_path)))[0]["id"]

        updated = await tool.execute(
            {
                "action": "update",
                "entry_id": entry_id,
                "content": "sqlite fts5 включен",
                "source": "agent",
                "verification_status": "verified",
            },
            ctx,
        )

        assert updated["success"] is False
        entry = parse_entries(read_memory(str(tmp_path)))[0]
        assert entry["text"] == "sqlite включен"
        assert entry["verification_status"] == "verified"
        assert entry["evidence_type"] == "config"

    asyncio.run(_run())


def test_memory_plugin_verified_update_same_source_without_new_evidence_downgrades_trust(tmp_path):
    async def _run():
        tool = MemoryTool()
        ctx = {"cwd": str(tmp_path), "state_root": str(tmp_path)}
        await tool.execute(
            {
                "action": "append",
                "tag": "DECISION",
                "content": "использовать sqlite",
                "layer": "semantic",
                "verification_status": "verified",
                "evidence_type": "config",
                "evidence_ref": "config.yaml",
            },
            ctx,
        )
        entry_id = parse_entries(read_memory(str(tmp_path)))[0]["id"]

        updated = await tool.execute(
            {
                "action": "update",
                "entry_id": entry_id,
                "content": "использовать postgres",
                "source": "agent",
            },
            ctx,
        )

        assert updated["success"] is True
        entry = parse_entries(read_memory(str(tmp_path)))[0]
        assert entry["text"] == "использовать postgres"
        assert entry["verification_status"] == "unverified"
        assert entry["evidence_type"] == "none"

    asyncio.run(_run())
