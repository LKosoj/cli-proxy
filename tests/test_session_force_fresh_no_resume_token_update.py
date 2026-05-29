import asyncio

from config import ToolConfig
from session import Session


def test_force_fresh_does_not_update_resume_token(tmp_path):
    tool = ToolConfig(
        name="bash",
        mode="headless",
        cmd=["bash", "-lc", "echo '{\"thread_id\":\"NEW_TOKEN\"}'"],
        resume_regex=r'"thread_id"\s*:\s*"([^"]+)"',
    )
    s = Session(
        id="s1",
        tool=tool,
        workdir=str(tmp_path),
        idle_timeout_sec=5,
        config=None,  # not used in this test
    )
    # Non-fresh: resume_token may update based on output when it is not set yet.
    s.resume_token = None
    asyncio.run(s.run_prompt("x"))
    assert s.resume_token == "NEW_TOKEN"

    # Fresh: must not update resume_token even if output contains a token.
    s.resume_token = "OLD_TOKEN"
    asyncio.run(s.run_prompt("x", force_fresh=True))
    assert s.resume_token == "OLD_TOKEN"
