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
