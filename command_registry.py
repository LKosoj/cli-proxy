from typing import Dict, List


def build_command_registry(bot_app) -> List[Dict[str, object]]:
    mtproto_enabled = bool(getattr(bot_app.config, "mtproto", None) and bot_app.config.mtproto.enabled)
    return [
        {"name": "new", "desc": "Создать новую сессию (через меню).", "handler": bot_app.cmd_new, "menu": True},
        {"name": "sessions", "desc": "Меню управления сессиями.", "handler": bot_app.cmd_sessions, "menu": True},
        {"name": "interrupt", "desc": "Прервать текущую генерацию.", "handler": bot_app.cmd_interrupt, "menu": True},
        {"name": "git", "desc": "Git-операции по активной сессии (inline-меню).", "handler": bot_app.cmd_git, "menu": True},
        {"name": "mtproto", "desc": "MTProto меню для отправки сообщений в заданные чаты.", "handler": bot_app.cmd_mtproto, "menu": mtproto_enabled},
        {"name": "files", "desc": "Отправить файл из рабочей директории.", "handler": bot_app.cmd_files, "menu": True},
        {"name": "agent", "desc": "Включить/выключить агента для активной сессии.", "handler": bot_app.cmd_agent, "menu": True},
        {"name": "preset", "desc": "Шаблоны задач для CLI.", "handler": bot_app.cmd_preset, "menu": False},
        {"name": "metrics", "desc": "Метрики бота.", "handler": bot_app.cmd_metrics, "menu": False},
        {"name": "tools", "desc": "Показать доступные инструменты.", "handler": bot_app.cmd_tools, "menu": True},
        {"name": "toolhelp", "desc": "Показать /команды выбранного инструмента.", "handler": bot_app.cmd_toolhelp, "menu": True},
        {"name": "newpath", "desc": "Задать путь для новой сессии после выбора инструмента.", "handler": bot_app.cmd_newpath, "menu": False},
        {"name": "use", "desc": "Выбрать активную сессию (через меню).", "handler": bot_app.cmd_use, "menu": False},
        {"name": "close", "desc": "Закрыть сессию (через меню).", "handler": bot_app.cmd_close, "menu": False},
        {"name": "status", "desc": "Показать статус активной сессии.", "handler": bot_app.cmd_status, "menu": False},
        {"name": "queue", "desc": "Показать очередь.", "handler": bot_app.cmd_queue, "menu": False},
        {"name": "clearqueue", "desc": "Очистить очередь активной сессии.", "handler": bot_app.cmd_clearqueue, "menu": False},
        {"name": "rename", "desc": "Переименовать сессию.", "handler": bot_app.cmd_rename, "menu": False},
        {"name": "cwd", "desc": "Создать новую сессию в другом каталоге.", "handler": bot_app.cmd_cwd, "menu": False},
        {"name": "dirs", "desc": "Просмотр каталогов (меню).", "handler": bot_app.cmd_dirs, "menu": False},
        {"name": "resume", "desc": "Показать/установить resume токен.", "handler": bot_app.cmd_resume, "menu": False},
        {"name": "state", "desc": "Просмотр состояния (меню).", "handler": bot_app.cmd_state, "menu": False},
        {"name": "setprompt", "desc": "Установить prompt_regex для инструмента.", "handler": bot_app.cmd_setprompt, "menu": False},
        {"name": "send", "desc": "Отправить текст напрямую в CLI.", "handler": bot_app.cmd_send, "menu": False},
    ]
