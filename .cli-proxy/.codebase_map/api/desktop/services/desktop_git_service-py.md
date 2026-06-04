# API Spec: `desktop/services/desktop_git_service.py`

Generated: 2026-06-04T00:00:00Z

## Type aliases
- `GitOpResult = Dict[str, Any]` — структурированный результат merge/rebase:
  `{"status": "ok"|"conflict"|"error", "code": int, "output": str, "files": list[str]?}`

## Classes
### `class _GitResult` (line 22)

### `class DesktopGitService` (line 28)
*Минимальный Git-сервис для Desktop UI.*
- `def __init__(*, timeout_sec, logger, ssh_service, remote_control_service, github_token)` (line 39)
  - `github_token` — опциональный GitHub PAT для аутентификации push/pull/fetch через GIT_ASKPASS.
- `async def status_text(session)` (line 55)
- `async def get_commit_context(session)` (line 74)
  - *Контекст для генерации сообщения коммита (status + diff), как в боте.*
- `async def commit(session, message, body)` (line 95)
- `def _ensure_askpass()` (line 111) — создаёт временный GIT_ASKPASS-скрипт (кешируется).
- `def _git_env()` (line 133) — строит env с GIT_TERMINAL_PROMPT=0 + опциональный ASKPASS.
- `async def _current_branch(session)` (line 150)
- `async def _upstream(session)` (line 157)
- `async def pull(session, *, strategy, remote)` (line 168)
  - strategy: "ff" (default) | "merge" | "rebase"
- `async def push(session, *, remote)` (line 190)
  - auto-upstream: добавляет `-u origin <branch>` если upstream не настроен.
- `async def fetch(session, remote)` (line 203) → `tuple[int, str]`
  - Запускает `git fetch --prune <remote>`.
- `async def merge(session, branch, *, strategy)` (line 207) → `GitOpResult`
  - strategy: "ff" | "merge" | "squash"
- `async def rebase(session, target_branch)` (line 236) → `GitOpResult`
- `@staticmethod def _classify_git_result(code, output)` (line 248) → `GitOpResult`
- `async def _run_git_authed(session, args)` (line 264) — как _run_git, но с token env.
- `async def log(session, max_count, fmt)` (line 308)
  - *Возвращает историю коммитов в формате, удобном для парсинга.*
- `async def show(session, commit)` (line 337)
  - *Показывает diff конкретного коммита.*
- `async def stash(session)` (line 341)
- `async def stash_pop(session)` (line 344)
- `async def branch_create(session, branch_name)` (line 347)
- `async def checkout(session, branch_name)` (line 350)
