import asyncio
import types

from modes.webmaster.mode import WebmasterMode


def _make_patch(i: int) -> dict[str, str]:
    return {
        "added_rules": f"rule-{i}",
        "changed_rules": "",
        "removed_rules": "",
        "reason": f"r-{i}",
        "expected_effect": f"e-{i}",
    }


def test_webmaster_prompt_compaction_not_triggered_for_20(tmp_path) -> None:
    async def _run() -> None:
        mode = WebmasterMode()
        session = types.SimpleNamespace(workdir=str(tmp_path))
        called = {"v": False}

        async def _fake_compact(_self, _bot_app, _patches, *, session):
            called["v"] = True
            return _make_patch(999)

        mode._compact_prompt_patches_llm = types.MethodType(_fake_compact, mode)
        learning = {"patches": [_make_patch(i) for i in range(20)], "active_version": 7}
        out = await mode._maybe_compact_prompt_learning(
            types.SimpleNamespace(),
            learning,
            session=session,
        )

        assert called["v"] is False
        assert len(out["patches"]) == 20
        assert out["patches"][0]["added_rules"] == ["rule-0"]
        assert out["active_version"] == 7

    asyncio.run(_run())


def test_webmaster_prompt_compaction_triggered_for_21(tmp_path) -> None:
    async def _run() -> None:
        mode = WebmasterMode()
        session = types.SimpleNamespace(workdir=str(tmp_path))

        async def _fake_compact(_self, _bot_app, patches, *, session):
            assert len(patches) == 21
            return _make_patch(1001)

        mode._compact_prompt_patches_llm = types.MethodType(_fake_compact, mode)
        learning = {"patches": [_make_patch(i) for i in range(21)], "active_version": 9}
        out = await mode._maybe_compact_prompt_learning(
            types.SimpleNamespace(),
            learning,
            session=session,
        )

        assert len(out["patches"]) == 1
        assert out["patches"][0]["added_rules"] == ["rule-1001"]
        assert out["active_version"] == 9

    asyncio.run(_run())


def test_webmaster_prompt_compaction_failure_keeps_all_patches(tmp_path) -> None:
    async def _run() -> None:
        mode = WebmasterMode()
        session = types.SimpleNamespace(workdir=str(tmp_path))

        async def _fake_compact(_self, _bot_app, _patches, *, session):
            raise RuntimeError("llm down")

        mode._compact_prompt_patches_llm = types.MethodType(_fake_compact, mode)
        learning = {"patches": [_make_patch(i) for i in range(37)], "active_version": 5}
        out = await mode._maybe_compact_prompt_learning(
            types.SimpleNamespace(),
            learning,
            session=session,
        )

        assert len(out["patches"]) == 37
        assert out["patches"][0]["added_rules"] == ["rule-0"]
        assert out["patches"][-1]["added_rules"] == ["rule-36"]
        assert out["active_version"] == 5

    asyncio.run(_run())
