import asyncio
import types
from unittest.mock import AsyncMock

from app.services.git_ops_service import GitOps
from tg.handlers import BotHandlers


def _build_git_ops(*, manager=None, send_message=None) -> GitOps:
    return GitOps(
        config=types.SimpleNamespace(
            defaults=types.SimpleNamespace(
                github_token=None,
                openai_api_key=None,
            )
        ),
        manager=manager or types.SimpleNamespace(get_by_scope=lambda *_args, **_kwargs: None),
        send_message=send_message or AsyncMock(),
        edit_message=AsyncMock(),
        send_document=AsyncMock(),
        short_label=lambda value: str(value),
        handle_cli_input=AsyncMock(),
    )


def test_cmd_git_passes_thread_id_and_replies_in_same_topic() -> None:
    async def _run() -> None:
        git = types.SimpleNamespace(
            ensure_git_session=AsyncMock(return_value=types.SimpleNamespace(id="s1")),
            ensure_git_repo=AsyncMock(return_value=True),
            build_git_keyboard=lambda: "git-keyboard",
        )
        bot_app = types.SimpleNamespace(
            git=git,
            _send_message=AsyncMock(),
            resolve_telegram_inbound_route=lambda _update: types.SimpleNamespace(
                reply_chat_id=101,
                message_thread_id=55,
                reply_kwargs=lambda: {"chat_id": 101, "message_thread_id": 55},
            ),
            build_telegram_reply_dest=lambda *_args, **_kwargs: {"chat_id": 999},
        )
        handlers = BotHandlers(bot_app)
        handlers._ensure_allowed = AsyncMock(return_value=True)  # type: ignore[method-assign]
        handlers._require_admin = AsyncMock(return_value=True)  # type: ignore[method-assign]

        update = types.SimpleNamespace(effective_chat=types.SimpleNamespace(id=101))
        context = types.SimpleNamespace()

        await handlers.cmd_git(update, context)

        git.ensure_git_session.assert_awaited_once_with(101, context, message_thread_id=55)
        git.ensure_git_repo.assert_awaited_once_with(
            git.ensure_git_session.return_value,
            101,
            context,
            message_thread_id=55,
        )
        send_kwargs = bot_app._send_message.await_args.kwargs
        assert send_kwargs["chat_id"] == 101
        assert send_kwargs["message_thread_id"] == 55
        assert send_kwargs["reply_markup"] == "git-keyboard"

    asyncio.run(_run())


def test_git_ops_ensure_git_session_reports_missing_context_in_same_topic() -> None:
    async def _run() -> None:
        send_message = AsyncMock()
        ops = _build_git_ops(send_message=send_message)

        session = await ops.ensure_git_session(101, types.SimpleNamespace(), message_thread_id=55)

        assert session is None
        send_kwargs = send_message.await_args.kwargs
        assert send_kwargs["chat_id"] == 101
        assert send_kwargs["message_thread_id"] == 55
        assert "Сессия для текущего контекста не найдена" in send_kwargs["text"]

    asyncio.run(_run())


def test_git_branch_menu_state_is_thread_aware() -> None:
    ops = _build_git_ops()
    ops.git_branch_menu[ops._state_key(101, 55)] = ["origin/main"]
    ops.git_branch_menu[ops._state_key(101, 56)] = ["origin/dev"]

    keyboard_a = ops._build_git_branches_keyboard(101, "merge", message_thread_id=55)
    keyboard_b = ops._build_git_branches_keyboard(101, "merge", message_thread_id=56)

    assert keyboard_a.inline_keyboard[0][0].text == "origin/main"
    assert keyboard_b.inline_keyboard[0][0].text == "origin/dev"


def test_git_conflict_check_does_not_treat_worktree_warning_as_conflict() -> None:
    async def _run() -> None:
        ops = _build_git_ops()
        session = types.SimpleNamespace(
            git=types.SimpleNamespace(conflict=False, conflict_files=[], conflict_kind=None)
        )

        async def _run_git(_session, args):
            if args[0] == "diff":
                return 0, "warning: LF will be replaced by CRLF"
            return 0, ""

        ops._run_git = AsyncMock(side_effect=_run_git)

        conflicts = await ops._git_conflict_files(session)

        assert conflicts == []
        ops._run_git.assert_awaited_once_with(
            session,
            ["ls-files", "--unmerged", "--format=%(path)"],
        )

    asyncio.run(_run())


def test_git_commit_conflict_message_stays_in_callback_topic() -> None:
    async def _run() -> None:
        ops = _build_git_ops()
        session = types.SimpleNamespace(id="s7")
        ops.ensure_git_session = AsyncMock(return_value=session)
        ops.ensure_git_repo = AsyncMock(return_value=True)
        ops.ensure_git_not_busy = AsyncMock(return_value=True)
        ops._git_conflict_files = AsyncMock(return_value=["file.txt"])
        ops._handle_git_conflict = AsyncMock()

        query = types.SimpleNamespace(
            data="git_commit",
            message=types.SimpleNamespace(chat_id=101, message_id=10, message_thread_id=55),
        )
        context = types.SimpleNamespace()

        handled = await ops.handle_callback(query, 101, context)

        assert handled is True
        ops._handle_git_conflict.assert_awaited_once_with(
            session,
            101,
            context,
            message_thread_id=55,
            lang="ru",
        )

    asyncio.run(_run())
