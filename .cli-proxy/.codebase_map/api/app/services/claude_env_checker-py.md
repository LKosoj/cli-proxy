# API Spec: `app/services/claude_env_checker.py`

Generated: 2026-06-03T02:24:29Z

## Classes
### `class CheckResult` (line 20)
*Результат отдельной проверки.*

### `class EnvCheckResult` (line 29)
*Общий результат проверки окружения.*
- `def is_claude_available()` (line 40)
  - *Проверяет, готово ли окружение для запуска claude.*

### `class ClaudeEnvChecker` (line 50)
*Проверка окружения для запуска CLI-агентов от имени claude-bot.*
- `def __init__(workdir, username, claude_binary)` (line 63)
- `def check_user_exists()` (line 100)
  - *Проверить существование пользователя.*
- `def check_claude_installed()` (line 147)
  - *Проверить наличие установленного claude.*
- `def check_workdir_accessible()` (line 174)
  - *Проверить доступность workdir для записи.*
- `def check_path_configured()` (line 192)
  - *Проверить, что ~/.local/bin в PATH.*
- `def check_all()` (line 224)
  - *Выполнить все проверки.*

## Symbols
- `def check_claude_env(workdir, username)` (line 278)
  - *Удобная функция для проверки окружения claude.*
- `def format_check_result(result)` (line 293)
  - *Отформатировать результат проверки для вывода пользователю.*
