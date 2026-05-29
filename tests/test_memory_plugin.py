import asyncio
import re

from agent.plugins.memory import MemoryTool


def test_memory_plugin_append_uses_default_tag(tmp_path):
    async def _run():
        tool = MemoryTool()
        ctx = {"cwd": str(tmp_path), "state_root": str(tmp_path)}
        resp = await tool.execute({"action": "append", "content": "запомни это"}, ctx)
        assert resp.get("success") is True
        content = (tmp_path / "MEMORY.md").read_text(encoding="utf-8")
        assert re.search(
            r"^- \d{4}-\d{2}-\d{2} \d{2}:\d{2}: \[AGREEMENT\].*\[LAYER:semantic\].*\[ID:[a-f0-9]{12}\] запомни это$",
            content.strip(),
        )

    asyncio.run(_run())


def test_memory_plugin_append_accepts_category_alias_tag(tmp_path):
    async def _run():
        tool = MemoryTool()
        ctx = {"cwd": str(tmp_path), "state_root": str(tmp_path)}
        resp = await tool.execute({"action": "append", "content": "параметр проекта", "tag": "config"}, ctx)
        assert resp.get("success") is True
        content = (tmp_path / "MEMORY.md").read_text(encoding="utf-8")
        assert "[CONFIG]" in content
        assert "параметр проекта" in content

    asyncio.run(_run())


def test_memory_plugin_append_rejects_invalid_tag(tmp_path):
    async def _run():
        tool = MemoryTool()
        ctx = {"cwd": str(tmp_path), "state_root": str(tmp_path)}
        resp = await tool.execute({"action": "append", "content": "x", "tag": "random"}, ctx)
        assert resp.get("success") is False
        assert "Invalid tag" in str(resp.get("error") or "")

    asyncio.run(_run())


def test_memory_plugin_update_and_forget_by_id(tmp_path):
    async def _run():
        tool = MemoryTool()
        ctx = {"cwd": str(tmp_path), "state_root": str(tmp_path)}
        resp_add = await tool.execute({"action": "append", "content": "старый факт", "tag": "agreement"}, ctx)
        assert resp_add.get("success") is True
        content_before = (tmp_path / "MEMORY.md").read_text(encoding="utf-8")
        m = re.search(r"\[ID:([a-f0-9]{12})\]", content_before)
        assert m is not None
        entry_id = m.group(1)

        resp_upd = await tool.execute(
            {"action": "update", "entry_id": entry_id, "content": "обновленный факт", "source": "agent"},
            ctx,
        )
        assert resp_upd.get("success") is True
        content_after_upd = (tmp_path / "MEMORY.md").read_text(encoding="utf-8")
        assert "обновленный факт" in content_after_upd

        resp_forget = await tool.execute({"action": "forget", "entry_id": entry_id}, ctx)
        assert resp_forget.get("success") is True
        content_after_del = (tmp_path / "MEMORY.md").read_text(encoding="utf-8")
        assert "обновленный факт" not in content_after_del

    asyncio.run(_run())


def test_memory_plugin_append_rejects_non_atomic_content(tmp_path):
    async def _run():
        tool = MemoryTool()
        ctx = {"cwd": str(tmp_path), "state_root": str(tmp_path)}
        too_long = "x" * 400
        resp = await tool.execute({"action": "append", "content": too_long, "tag": "agreement"}, ctx)
        assert resp.get("success") is False
        assert "atomic" in str(resp.get("error") or "")

    asyncio.run(_run())
