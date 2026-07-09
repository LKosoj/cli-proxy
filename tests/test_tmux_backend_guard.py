from pathlib import Path


TMUX_BACKEND_FILES = [
    Path("app/services/cli_backends/tmux_backend.py"),
    Path("app/services/cli_backends/tmux_driver.py"),
    Path("app/services/cli_backends/tmux_parser.py"),
]


def test_tmux_backend_has_no_headless_claude_invocation() -> None:
    combined = "\n".join(path.read_text(encoding="utf-8") for path in TMUX_BACKEND_FILES)

    forbidden_literals = [
        "claude -p",
        '"claude", "-p"',
        "'claude', '-p'",
        "headless_cmd",
    ]
    for literal in forbidden_literals:
        assert literal not in combined
