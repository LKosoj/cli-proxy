from __future__ import annotations


class ErrorMessageService:
    """Unified SDK layer for mode-facing error/status texts."""

    @staticmethod
    def manager_archive_failed() -> str:
        return "Не удалось перенести план в архив."

    @staticmethod
    def manager_runtime_unavailable() -> str:
        return "Manager runtime недоступен."

    @staticmethod
    def manager_resume_failed() -> str:
        return "Не удалось возобновить план."

    @staticmethod
    def stale_choice_resend_task() -> str:
        return "Выбор устарел. Пришлите задачу заново."
