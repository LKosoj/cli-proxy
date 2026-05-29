import asyncio
import types

from app.services.project_prompts_service import ensure_project_prompts
from modes.manager.mode import ManagerMode
from modes.sdk import MessagingService
from modes.sdk.planning import ManagerDecomposeNormalizationError


class _FailingRuntime:
    async def run(self, _session, _prompt, _bot_app, _context, _dest):
        raise ManagerDecomposeNormalizationError("Ошибка декомпозиции: пришлите задачу в формате outcome/ограничения/проверки.")


def test_manager_mode_run_pipeline_handles_decompose_error_and_notifies_user(tmp_path) -> None:
    ensure_project_prompts(str(tmp_path))

    async def _run() -> None:
        sent = []

        async def _send_message(_context, *, chat_id: int, text: str, **_kwargs):
            sent.append((chat_id, text))
            return None

        bot_app = types.SimpleNamespace(_send_message=_send_message)
        mode = ManagerMode()
        mode.initialize(
            config=types.SimpleNamespace(),
            services={
                "runtime_by_capability": (
                    lambda capability: _FailingRuntime() if capability == "run_manager" else None
                ),
                "messaging_factory": (
                    lambda ctx: MessagingService(
                        send_message=bot_app._send_message,
                        transport_context=ctx,
                    )
                ),
            },
        )
        session = types.SimpleNamespace(workdir=str(tmp_path))

        result = await mode.run_pipeline(
            session=session,
            user_text="Сделай фичу",
            bot_app=bot_app,
            context=object(),
            dest={"kind": "telegram", "chat_id": 321},
        )

        expected = (
            "Не удалось построить план: ответ декомпозиции не распознан как валидный JSON-план.\n"
            "Пришлите задачу заново в формате: outcome, ограничения, проверки."
        )
        assert result == expected
        assert sent
        assert sent[-1][0] == 321
        assert sent[-1][1] == expected

    asyncio.run(_run())
