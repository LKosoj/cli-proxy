import asyncio

from config import ToolConfig
from session import Session


def test_image_run_uses_non_resume_template_when_session_has_no_token(tmp_path, monkeypatch) -> None:
    async def _run() -> None:
        tool = ToolConfig(
            name="codex",
            mode="headless",
            cmd=["codex", "exec", "{prompt}"],
            headless_cmd=["codex", "exec", "{prompt}"],
            resume_cmd=["codex", "exec", "resume", "{resume}", "{prompt}"],
            image_cmd=["--image", "{image}"],
        )
        session = Session(
            id="s1",
            tool=tool,
            workdir=str(tmp_path),
            idle_timeout_sec=10,
            config=None,
        )
        session.resume_token = None
        captured = {}

        async def _fake_run_headless(prompt: str, cmd_template=None, image_path=None, *, force_fresh: bool = False):
            captured["prompt"] = prompt
            captured["cmd_template"] = list(cmd_template or [])
            captured["image_path"] = image_path
            captured["force_fresh"] = force_fresh
            return "ok"

        monkeypatch.setattr(session, "_run_headless", _fake_run_headless)

        out = await session.run_prompt("hello", image_path="/tmp/image.png")
        assert out == "ok"
        assert captured["prompt"] == "hello"
        assert captured["image_path"] == "/tmp/image.png"
        assert captured["cmd_template"] == ["codex", "exec", "{prompt}", "--image", "{image}"]

    asyncio.run(_run())


def test_image_run_uses_resume_template_when_token_exists(tmp_path, monkeypatch) -> None:
    async def _run() -> None:
        tool = ToolConfig(
            name="codex",
            mode="headless",
            cmd=["codex", "exec", "{prompt}"],
            headless_cmd=["codex", "exec", "{prompt}"],
            resume_cmd=["codex", "exec", "resume", "{resume}", "{prompt}"],
            image_cmd=["--image", "{image}"],
        )
        session = Session(
            id="s1",
            tool=tool,
            workdir=str(tmp_path),
            idle_timeout_sec=10,
            config=None,
        )
        session.resume_token = "r1"
        captured = {}

        async def _fake_run_headless(prompt: str, cmd_template=None, image_path=None, *, force_fresh: bool = False):
            captured["cmd_template"] = list(cmd_template or [])
            return "ok"

        monkeypatch.setattr(session, "_run_headless", _fake_run_headless)

        out = await session.run_prompt("hello", image_path="/tmp/image.png")
        assert out == "ok"
        assert captured["cmd_template"] == ["codex", "exec", "resume", "{resume}", "{prompt}", "--image", "{image}"]

    asyncio.run(_run())


def test_gemini_image_run_injects_attachment_into_prompt(tmp_path, monkeypatch) -> None:
    async def _run() -> None:
        tool = ToolConfig(
            name="gemini",
            mode="headless",
            cmd=["gemini", "-p", "{prompt}"],
            headless_cmd=["gemini", "-p", "{prompt}"],
        )
        session = Session(
            id="s1",
            tool=tool,
            workdir=str(tmp_path),
            idle_timeout_sec=10,
            config=None,
        )
        captured = {}

        async def _fake_run_headless(prompt: str, cmd_template=None, image_path=None, *, force_fresh: bool = False):
            captured["prompt"] = prompt
            captured["cmd_template"] = list(cmd_template or [])
            captured["image_path"] = image_path
            return "ok"

        monkeypatch.setattr(session, "_run_headless", _fake_run_headless)

        out = await session.run_prompt("что на изображении?", image_path="/tmp/image.png")
        assert out == "ok"
        assert captured["prompt"] == "@/tmp/image.png\nчто на изображении?"
        assert captured["cmd_template"] == ["gemini", "-p", "{prompt}"]
        # Нативного флага у gemini нет, путь уходит только @-ссылкой в промпте.
        assert captured["image_path"] is None

    asyncio.run(_run())


def test_gemini_image_run_allows_empty_caption(tmp_path, monkeypatch) -> None:
    async def _run() -> None:
        tool = ToolConfig(
            name="gemini",
            mode="headless",
            cmd=["gemini", "-p", "{prompt}"],
            headless_cmd=["gemini", "-p", "{prompt}"],
        )
        session = Session(
            id="s1",
            tool=tool,
            workdir=str(tmp_path),
            idle_timeout_sec=10,
            config=None,
        )
        captured = {}

        async def _fake_run_headless(prompt: str, cmd_template=None, image_path=None, *, force_fresh: bool = False):
            captured["prompt"] = prompt
            return "ok"

        monkeypatch.setattr(session, "_run_headless", _fake_run_headless)

        out = await session.run_prompt("", image_path="/tmp/image.png")
        assert out == "ok"
        assert captured["prompt"] == "@/tmp/image.png"

    asyncio.run(_run())


def test_gemini_image_run_uses_explicit_resume_token_when_available(tmp_path, monkeypatch) -> None:
    async def _run() -> None:
        tool = ToolConfig(
            name="gemini",
            mode="headless",
            cmd=["gemini", "--approval-mode", "yolo", "--resume", "latest", "-p", "{prompt}"],
            headless_cmd=["gemini", "--approval-mode", "yolo", "--resume", "latest", "-p", "{prompt}"],
        )
        session = Session(
            id="s1",
            tool=tool,
            workdir=str(tmp_path),
            idle_timeout_sec=10,
            config=None,
        )
        session.resume_token = "gem-token-123"
        captured = {}

        async def _fake_run_headless(prompt: str, cmd_template=None, image_path=None, *, force_fresh: bool = False):
            captured["prompt"] = prompt
            captured["cmd_template"] = list(cmd_template or [])
            return "ok"

        monkeypatch.setattr(session, "_run_headless", _fake_run_headless)

        out = await session.run_prompt("опиши изображение", image_path="/tmp/image.png")
        assert out == "ok"
        assert captured["prompt"] == "@/tmp/image.png\nопиши изображение"
        assert captured["cmd_template"] == [
            "gemini",
            "--approval-mode",
            "yolo",
            "--resume",
            "{resume}",
            "-p",
            "{prompt}",
        ]

    asyncio.run(_run())


def test_qwen_image_run_uses_vision_model_and_prompt_attachment(tmp_path, monkeypatch) -> None:
    async def _run() -> None:
        workdir = tmp_path / "repo"
        workdir.mkdir()
        tool = ToolConfig(
            name="qwen",
            mode="headless",
            cmd=["qwen", "--yolo", "--continue", "--prompt", "{prompt}", "--resume", "{resume}"],
            headless_cmd=["qwen", "--yolo", "--continue", "--prompt", "{prompt}", "--resume", "{resume}"],
        )
        session = Session(
            id="s1",
            tool=tool,
            workdir=str(workdir),
            idle_timeout_sec=10,
            config=None,
        )
        captured = {}
        image_path = str(workdir / ".cli-proxy" / ".attachments" / "diagram.png")

        async def _fake_run_headless(prompt: str, cmd_template=None, image_path=None, *, force_fresh: bool = False):
            captured["prompt"] = prompt
            captured["cmd_template"] = list(cmd_template or [])
            captured["image_path"] = image_path
            return "ok"

        monkeypatch.setattr(session, "_run_headless", _fake_run_headless)

        out = await session.run_prompt("Расскажи о", image_path=image_path)
        assert out == "ok"
        assert captured["prompt"] == "Расскажи о @.cli-proxy/.attachments/diagram.png"
        assert captured["cmd_template"] == ["qwen", "{prompt}", "--model", "vision-model"]
        assert captured["image_path"] is None

    asyncio.run(_run())


def test_qwen_image_run_uses_default_text_when_caption_empty(tmp_path, monkeypatch) -> None:
    async def _run() -> None:
        workdir = tmp_path / "repo"
        workdir.mkdir()
        tool = ToolConfig(
            name="qwen",
            mode="headless",
            cmd=["qwen", "--prompt", "{prompt}"],
            headless_cmd=["qwen", "--prompt", "{prompt}"],
        )
        session = Session(
            id="s1",
            tool=tool,
            workdir=str(workdir),
            idle_timeout_sec=10,
            config=None,
        )
        captured = {}
        image_path = str(workdir / ".cli-proxy" / ".attachments" / "diagram.png")

        async def _fake_run_headless(prompt: str, cmd_template=None, image_path=None, *, force_fresh: bool = False):
            captured["prompt"] = prompt
            return "ok"

        monkeypatch.setattr(session, "_run_headless", _fake_run_headless)

        out = await session.run_prompt("", image_path=image_path)
        assert out == "ok"
        assert captured["prompt"] == "Расскажи о @.cli-proxy/.attachments/diagram.png"

    asyncio.run(_run())


def test_qwen_image_run_uses_resume_when_token_exists(tmp_path, monkeypatch) -> None:
    async def _run() -> None:
        workdir = tmp_path / "repo"
        workdir.mkdir()
        tool = ToolConfig(
            name="qwen",
            mode="headless",
            cmd=["qwen", "--yolo", "--continue", "--prompt", "{prompt}", "--resume", "{resume}"],
            headless_cmd=["qwen", "--yolo", "--continue", "--prompt", "{prompt}", "--resume", "{resume}"],
        )
        session = Session(
            id="s1",
            tool=tool,
            workdir=str(workdir),
            idle_timeout_sec=10,
            config=None,
        )
        session.resume_token = "qwen-session-1"
        captured = {}
        image_path = str(workdir / ".cli-proxy" / ".attachments" / "diagram.png")

        async def _fake_run_headless(prompt: str, cmd_template=None, image_path=None, *, force_fresh: bool = False):
            captured["prompt"] = prompt
            captured["cmd_template"] = list(cmd_template or [])
            return "ok"

        monkeypatch.setattr(session, "_run_headless", _fake_run_headless)

        out = await session.run_prompt("Расскажи о", image_path=image_path)
        assert out == "ok"
        assert captured["prompt"] == "Расскажи о @.cli-proxy/.attachments/diagram.png"
        assert captured["cmd_template"] == ["qwen", "{prompt}", "--model", "vision-model", "--resume", "{resume}"]

    asyncio.run(_run())


def test_codex_multi_image_run_joins_paths_for_image_flag(tmp_path, monkeypatch) -> None:
    async def _run() -> None:
        tool = ToolConfig(
            name="codex",
            mode="headless",
            cmd=["codex", "exec", "{prompt}"],
            headless_cmd=["codex", "exec", "{prompt}"],
            image_cmd=["--image", "{image}"],
        )
        session = Session(
            id="s1",
            tool=tool,
            workdir=str(tmp_path),
            idle_timeout_sec=10,
            config=None,
        )
        captured = {}

        async def _fake_run_headless(prompt: str, cmd_template=None, image_path=None, *, force_fresh: bool = False):
            captured["prompt"] = prompt
            captured["cmd_template"] = list(cmd_template or [])
            captured["image_path"] = image_path
            return "ok"

        monkeypatch.setattr(session, "_run_headless", _fake_run_headless)

        out = await session.run_prompt(
            "Сравни диаграммы",
            image_paths=["/tmp/img1.png", "/tmp/img2.jpg"],
        )
        assert out == "ok"
        assert captured["prompt"] == "Сравни диаграммы"
        assert captured["cmd_template"] == ["codex", "exec", "{prompt}", "--image", "{image}"]
        assert captured["image_path"] == "/tmp/img1.png,/tmp/img2.jpg"

    asyncio.run(_run())


def test_gemini_multi_image_run_injects_all_attachments(tmp_path, monkeypatch) -> None:
    async def _run() -> None:
        tool = ToolConfig(
            name="gemini",
            mode="headless",
            cmd=["gemini", "-p", "{prompt}"],
            headless_cmd=["gemini", "-p", "{prompt}"],
        )
        session = Session(
            id="s1",
            tool=tool,
            workdir=str(tmp_path),
            idle_timeout_sec=10,
            config=None,
        )
        captured = {}

        async def _fake_run_headless(prompt: str, cmd_template=None, image_path=None, *, force_fresh: bool = False):
            captured["prompt"] = prompt
            captured["image_path"] = image_path
            return "ok"

        monkeypatch.setattr(session, "_run_headless", _fake_run_headless)

        out = await session.run_prompt(
            "Сравни изображения",
            image_paths=["/tmp/a.png", "/tmp/b.png"],
        )
        assert out == "ok"
        assert captured["prompt"] == "@/tmp/a.png\n@/tmp/b.png\nСравни изображения"
        assert captured["image_path"] is None

    asyncio.run(_run())


def test_claude_image_run_injects_attachment_ref_into_prompt(tmp_path, monkeypatch) -> None:
    async def _run() -> None:
        workdir = tmp_path / "repo"
        workdir.mkdir()
        tool = ToolConfig(
            name="claude",
            mode="headless",
            cmd=["claude", "--continue", "-p", "{prompt}", "--resume", "{resume}"],
        )
        session = Session(
            id="s1",
            tool=tool,
            workdir=str(workdir),
            idle_timeout_sec=10,
            config=None,
        )
        captured = {}
        image_path = str(workdir / ".attachments" / "shot.png")

        async def _fake_run_headless(prompt: str, cmd_template=None, image_path=None, *, force_fresh: bool = False):
            captured["prompt"] = prompt
            captured["cmd_template"] = list(cmd_template or [])
            captured["image_path"] = image_path
            return "ok"

        monkeypatch.setattr(session, "_run_headless", _fake_run_headless)

        out = await session.run_prompt("опиши скрин", image_path=image_path)
        assert out == "ok"
        # Без @-ссылки путь к файлу нигде не появлялся: у claude нет ни image_cmd,
        # ни плейсхолдера {image} в шаблоне команды.
        assert captured["prompt"] == "опиши скрин @.attachments/shot.png"
        assert captured["cmd_template"] == ["claude", "--continue", "-p", "{prompt}", "--resume", "{resume}"]
        assert captured["image_path"] is None

    asyncio.run(_run())


def test_cli_without_image_flag_sends_attachment_refs_instead_of_error(tmp_path, monkeypatch) -> None:
    async def _run() -> None:
        workdir = tmp_path / "repo"
        workdir.mkdir()
        tool = ToolConfig(
            name="grok",
            mode="headless",
            cmd=["grok", "-p", "{prompt}"],
            headless_cmd=["grok", "-p", "{prompt}"],
        )
        session = Session(
            id="s1",
            tool=tool,
            workdir=str(workdir),
            idle_timeout_sec=10,
            config=None,
        )
        captured = {}

        async def _fake_run_headless(prompt: str, cmd_template=None, image_path=None, *, force_fresh: bool = False):
            captured["prompt"] = prompt
            return "ok"

        monkeypatch.setattr(session, "_run_headless", _fake_run_headless)

        out = await session.run_prompt(
            "сравни",
            image_paths=[str(workdir / ".attachments" / "a.png"), str(workdir / ".attachments" / "b.png")],
        )
        assert out == "ok"
        assert captured["prompt"] == "сравни @.attachments/a.png @.attachments/b.png"

    asyncio.run(_run())


def test_attachment_outside_workdir_keeps_absolute_ref(tmp_path, monkeypatch) -> None:
    async def _run() -> None:
        workdir = tmp_path / "repo"
        workdir.mkdir()
        tool = ToolConfig(
            name="kimi",
            mode="headless",
            cmd=["kimi", "--prompt", "{prompt}"],
        )
        session = Session(
            id="s1",
            tool=tool,
            workdir=str(workdir),
            idle_timeout_sec=10,
            config=None,
        )
        captured = {}

        async def _fake_run_headless(prompt: str, cmd_template=None, image_path=None, *, force_fresh: bool = False):
            captured["prompt"] = prompt
            return "ok"

        monkeypatch.setattr(session, "_run_headless", _fake_run_headless)

        out = await session.run_prompt("", image_path="/var/tmp/outside.png")
        assert out == "ok"
        assert captured["prompt"] == "Расскажи о @/var/tmp/outside.png"

    asyncio.run(_run())
