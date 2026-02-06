def test_trim_fetch_output_truncates_to_80k():
    from agent.tooling import helpers

    text = "x" * 90_000
    out = helpers._trim_fetch_output(text, reason="test")  # noqa: SLF001 (regression test)
    assert len(out) <= helpers.FETCH_MAX_CHARS
    assert "обрезано" in out


def test_trim_fetch_output_noop_when_small():
    from agent.tooling import helpers

    assert helpers._trim_fetch_output("hello", reason="test") == "hello"  # noqa: SLF001

