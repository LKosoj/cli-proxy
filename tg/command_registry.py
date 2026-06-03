from typing import Dict, List


def build_command_registry(bot_app) -> List[Dict[str, object]]:
    commands = [
        {
            "name": "start",
            "desc_key": "cmd.start.desc",
            "desc": "Показать ваш Telegram ID/Chat ID и подсказку по доступу.",
            "handler": bot_app.cmd_start,
            "menu": True,
        },
        {
            "name": "sessions",
            "desc_key": "cmd.sessions.desc",
            "desc": "Меню управления сессиями.",
            "handler": bot_app.cmd_sessions,
            "menu": True,
        },
        {
            "name": "interrupt",
            "desc_key": "cmd.interrupt.desc",
            "desc": "Прервать текущую генерацию.",
            "handler": bot_app.cmd_interrupt,
            "menu": True,
        },
        {
            "name": "git",
            "desc_key": "cmd.git.desc",
            "desc": "Git-операции по сессии текущего контекста (inline-меню).",
            "handler": bot_app.cmd_git,
            "menu": True,
        },
        {
            "name": "files",
            "desc_key": "cmd.files.desc",
            "desc": "Файлы сессии текущего контекста: отправка, сохранение, переименование, удаление.",
            "handler": bot_app.cmd_files,
            "menu": True,
        },
        {
            "name": "miniapp",
            "desc_key": "cmd.miniapp.desc",
            "desc": "Открыть MiniApp для администрирования.",
            "handler": bot_app.cmd_miniapp,
            "menu": True,
        },
        {
            "name": "selfupdate",
            "desc_key": "cmd.selfupdate.desc",
            "desc": "Обновить код бота из git и перезапустить сервис.",
            "handler": bot_app.cmd_selfupdate,
            "menu": True,
            "admin_only": True,
        },
        {
            "name": "lint_evolution_status",
            "desc_key": "cmd.lint_evolution_status.desc",
            "desc": "Lint Evolution: статус уровней L1/L2/L3, autopause, schema-version.",
            "handler": bot_app.cmd_lint_evolution_status,
            "menu": False,
            "admin_only": True,
        },
        {
            "name": "lint_autopause_resume",
            "desc_key": "cmd.lint_autopause_resume.desc",
            "desc": "Lint Evolution: снять autopause c уровня (1, 2 или 3).",
            "handler": bot_app.cmd_lint_autopause_resume,
            "menu": False,
            "admin_only": True,
        },
        {
            "name": "lint_schema_history",
            "desc_key": "cmd.lint_schema_history.desc",
            "desc": "Lint Evolution: текущая schema-версия, список полей, pending proposals.",
            "handler": bot_app.cmd_lint_schema_history,
            "menu": False,
            "admin_only": True,
        },
        {
            "name": "lint_gate_dry_run",
            "desc_key": "cmd.lint_gate_dry_run.desc",
            "desc": "Lint Evolution: прогон lint-gate по активным правилам в текущей сессии.",
            "handler": bot_app.cmd_lint_gate_dry_run,
            "menu": False,
            "admin_only": True,
        },
        {
            "name": "preset",
            "desc_key": "cmd.preset.desc",
            "desc": "Шаблоны задач для CLI.",
            "handler": bot_app.cmd_preset,
            "menu": False,
        },
        {
            "name": "metrics",
            "desc_key": "cmd.metrics.desc",
            "desc": "Метрики бота.",
            "handler": bot_app.cmd_metrics,
            "menu": False,
        },
        {
            "name": "tools",
            "desc_key": "cmd.tools.desc",
            "desc": "Показать доступные инструменты.",
            "handler": bot_app.cmd_tools,
            "menu": True,
        },
        {
            "name": "newpath",
            "desc_key": "cmd.newpath.desc",
            "desc": "Задать путь для новой сессии после выбора инструмента.",
            "handler": bot_app.cmd_newpath,
            "menu": False,
        },
        {
            "name": "close",
            "desc_key": "cmd.close.desc",
            "desc": "Закрыть сессию (через меню).",
            "handler": bot_app.cmd_close,
            "menu": False,
        },
        {
            "name": "status",
            "desc_key": "cmd.status.desc",
            "desc": "Показать статус сессии текущего контекста.",
            "handler": bot_app.cmd_status,
            "menu": False,
        },
        {
            "name": "limits",
            "desc_key": "cmd.limits.desc",
            "desc": "Показать лимиты/usage по CLI текущего чата.",
            "handler": bot_app.cmd_limits,
            "menu": True,
        },
        {
            "name": "queue",
            "desc_key": "cmd.queue.desc",
            "desc": "Показать очередь.",
            "handler": bot_app.cmd_queue,
            "menu": False,
        },
        {
            "name": "clearqueue",
            "desc_key": "cmd.clearqueue.desc",
            "desc": "Очистить очередь сессии текущего контекста.",
            "handler": bot_app.cmd_clearqueue,
            "menu": False,
        },
        {
            "name": "rename",
            "desc_key": "cmd.rename.desc",
            "desc": "Переименовать сессию.",
            "handler": bot_app.cmd_rename,
            "menu": False,
        },
        {
            "name": "cwd",
            "desc_key": "cmd.cwd.desc",
            "desc": "Создать новую сессию в другом каталоге.",
            "handler": bot_app.cmd_cwd,
            "menu": False,
        },
        {
            "name": "dirs",
            "desc_key": "cmd.dirs.desc",
            "desc": "Просмотр каталогов (меню).",
            "handler": bot_app.cmd_dirs,
            "menu": False,
        },
        {
            "name": "resume",
            "desc_key": "cmd.resume.desc",
            "desc": "Показать/установить resume токен.",
            "handler": bot_app.cmd_resume,
            "menu": False,
        },
        {
            "name": "state",
            "desc_key": "cmd.state.desc",
            "desc": "Просмотр состояния (меню).",
            "handler": bot_app.cmd_state,
            "menu": False,
        },
        {
            "name": "setprompt",
            "desc_key": "cmd.setprompt.desc",
            "desc": "Установить prompt_regex для инструмента.",
            "handler": bot_app.cmd_setprompt,
            "menu": False,
        },
        {
            "name": "send",
            "desc_key": "cmd.send.desc",
            "desc": "Отправить текст напрямую в CLI.",
            "handler": bot_app.cmd_send,
            "menu": False,
        },
    ]
    existing = {str(item.get("name") or "").strip() for item in commands}
    mode_service = getattr(bot_app, "mode_registry_service", None)
    if not mode_service or not hasattr(mode_service, "list_modes"):
        return commands
    mode_pairs = list(mode_service.list_modes() or [])
    mode_get = mode_service.get

    for mode_id, _label in mode_pairs:
        mode = mode_get(mode_id)
        if mode is None:
            continue
        command_name = str(mode_id or "").strip()
        if not command_name:
            continue
        if command_name in existing:
            continue
        label = str(getattr(mode, "display_name", "") or command_name).strip()

        async def _mode_handler(update, context, _mode_id=command_name):
            await bot_app.cmd_mode(update, context, _mode_id)

        commands.append(
            {
                "name": command_name,
                "desc_key": "cmd.mode.desc",
                "desc_params": {"label": label},
                "desc": f"Меню режима: {label}.",
                "handler": _mode_handler,
                "menu": command_name == "admin",
            }
        )
        existing.add(command_name)
    return commands
