# Дизайн: полный порт агента LocalTopSH + отдельный use_cli

## Цель
Полностью воспроизвести поведение LocalTopSH (ReAct, system prompt, инструменты, таймауты, блок‑листы, формат истории/памяти) в Python‑агенте, сохранив текущую CLI‑интеграцию бота. Отличие: `run_command` остаётся shell‑инструментом, а вызов CLI вынесен в отдельный tool `use_cli`.

## Архитектура и интеграция
- **Agent Core (Python)**: ReAct‑цикл, системный промпт 1:1, лимиты (maxIterations/maxHistory), blocked‑patterns, таймауты инструментов.
- **Tool Layer**: полный набор tools как в LocalTopSH, совпадающие схемы и форматы ответов.
- **CLI Tool**: отдельный `use_cli(task_text)` поверх текущего механизма CLI бота. CLI вызывается headless с параметрами активной сессии.
- **Session State**: `agent_enabled` хранится в файле сессий; восстанавливается при рестарте и переключении активной сессии.
- **UI**: `/agent` добавлен в главное меню сразу после `/files`, inline‑клавиатура для включения/выключения/отмены; `/status` показывает статус агента.

## Поток данных и ReAct‑цикл
1) Формирование prompt: `system.txt` + memory + history + текущий user input + working‑messages. Подстановка `{{tools}}` как в оригинале.
2) Вызов модели с `tool_choice: auto` и `tools`.
3) Если tool_calls отсутствуют — завершение; иначе последовательное выполнение tools.
4) Tool‑результаты добавляются как `role: tool` с `tool_call_id`.
5) История (`SESSION.json`) содержит только пары user/assistant без tool‑calls.

## Инструменты (1:1 с LocalTopSH)
Обязательные инструменты и контракты:
- `run_command` (shell‑команды, blocked‑patterns + timeout)
- `read_file`, `write_file`, `edit_file`, `delete_file`
- `search_files`, `search_text`, `list_directory`
- `search_web`, `fetch_page`
- `manage_tasks`, `schedule_task`
- `ask_user`
- `memory`
- `send_file`, `manage_message`, `get_meme`

Новый инструмент:
- `use_cli(task_text)` — вызывает CLI выбранного в сессии (codex/gemini/claude code), возвращает результат как `{ success, output?, error? }`.

## Безопасность и ошибки
- blocked‑patterns применяются до `run_command`.
- Таймауты для всех tools единые (`toolExecution`).
- Ошибки tools возвращаются в нормализованном виде, без падения агента.
- Превышение лимита блокировок завершает сессию агента с сообщением.

## Тестирование
- ReAct‑сценарий с 2–3 tool‑вызовами и корректным сохранением `SESSION.json`.
- Проверка `run_command` (не переключается на CLI).
- Проверка `use_cli` (headless, параметры активной сессии).
- `/agent` и `/status` (статус сохраняется между перезапусками).
- Формат ответов нескольких tools (search/read/write) совпадает с LocalTopSH.
