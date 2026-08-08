from utils import build_command


def test_build_command_keeps_following_arg_after_positional_resume_placeholder() -> None:
    cmd, use_stdin = build_command(
        ["codex", "exec", "resume", "{resume}", "--skip-git-repo-check", "{prompt}", "--image", "{image}"],
        prompt="hello",
        resume=None,
        image="/tmp/img.png",
    )

    assert cmd == ["codex", "exec", "resume", "--skip-git-repo-check", "hello", "--image", "/tmp/img.png"]
    assert use_stdin is False


def test_build_command_drops_resume_flag_and_value_when_resume_is_missing() -> None:
    cmd, use_stdin = build_command(
        ["qwen", "--continue", "--prompt", "{prompt}", "--resume", "{resume}"],
        prompt="hello",
        resume=None,
    )

    assert cmd == ["qwen", "--continue", "--prompt", "hello"]
    assert use_stdin is False


def test_build_command_kimi_swaps_continue_for_resume_token() -> None:
    template = [
        "kimi",
        "--output-format",
        "stream-json",
        "--continue",
        "--prompt",
        "{prompt}",
        "--resume",
        "{resume}",
    ]

    fresh, fresh_stdin = build_command(template, prompt="hello", resume=None)
    resumed, resumed_stdin = build_command(template, prompt="hello", resume="kimi-session-1")

    assert fresh == ["kimi", "--output-format", "stream-json", "--continue", "--prompt", "hello"]
    assert fresh_stdin is False
    # `--resume` у kimi - алиас `--session`, а он несовместим с `--continue`,
    # поэтому при токене в команде остаётся только resume.
    assert resumed == [
        "kimi",
        "--output-format",
        "stream-json",
        "--prompt",
        "hello",
        "--resume",
        "kimi-session-1",
    ]
    assert resumed_stdin is False


def test_build_command_opencode_drops_session_pair_before_positional_prompt() -> None:
    template = ["opencode", "run", "--format", "json", "--session", "{resume}", "{prompt}"]

    fresh, fresh_stdin = build_command(template, prompt="hello", resume=None)
    resumed, resumed_stdin = build_command(template, prompt="hello", resume="ses_1")

    # У opencode нет `--resume`, токен идёт в `--session`; без токена пара флаг +
    # значение выпадает целиком, иначе промпт уехал бы в значение флага.
    assert fresh == ["opencode", "run", "--format", "json", "hello"]
    assert fresh_stdin is False
    assert resumed == ["opencode", "run", "--format", "json", "--session", "ses_1", "hello"]
    assert resumed_stdin is False


def test_build_command_keeps_neighbour_flag_of_inline_resume_placeholder() -> None:
    # Слитая форма уносит только себя: соседний флаг не является её значением.
    cmd, _ = build_command(
        ["cli", "--yolo", "--resume={resume}", "{prompt}"],
        prompt="hello",
        resume=None,
    )

    assert cmd == ["cli", "--yolo", "hello"]
