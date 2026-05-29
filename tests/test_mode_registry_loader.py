import asyncio
from pathlib import Path

from modes.registry import ModeLoader, ModeRegistry


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def test_mode_loader_discovers_and_loads_plugins(tmp_path):
    root = tmp_path / "modes_root"
    # good plugin (class export)
    _write(
        root / "good" / "__init__.py",
        """\
from modes.sdk import BaseMode, ToolResult


class GoodMode(BaseMode):
    mode_id = "good"

    async def handle_input(self, message, ctx):
        return ToolResult.ok("x")

    async def handle_callback(self, callback, ctx):
        return ToolResult.ok("y")


PLUGIN = GoodMode
""",
    )
    # broken plugin import
    _write(
        root / "broken" / "__init__.py",
        "raise RuntimeError('boom')\n",
    )
    # plugin without PLUGIN export
    _write(
        root / "nop" / "__init__.py",
        "x = 1\n",
    )
    # sdk dir should be ignored even if present
    _write(
        root / "sdk" / "__init__.py",
        "PLUGIN = object()\n",
    )

    loader = ModeLoader(modes_dir=root, module_prefix="modes_test.dynamic")
    discovered = dict(loader.discover())
    assert "good" in discovered
    assert "broken" in discovered
    assert "nop" in discovered
    assert "sdk" not in discovered

    registry = ModeRegistry()
    loaded = loader.load_into(registry)
    assert loaded == 1
    assert registry.get("good") is not None
    assert registry.list_ids() == ["good"]


def test_mode_registry_rejects_duplicates():
    async def _run():
        from modes.sdk import BaseMode, ToolResult

        class M(BaseMode):
            mode_id = "dup"

            async def handle_input(self, message, ctx):
                return ToolResult.ok()

            async def handle_callback(self, callback, ctx):
                return ToolResult.ok()

        r = ModeRegistry()
        r.register(M())
        try:
            r.register(M())
            assert False, "expected ValueError"
        except ValueError:
            pass

    asyncio.run(_run())
