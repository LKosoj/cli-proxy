# API Spec: `desktop/services/desktop_git_service.py`

Generated: 2026-06-17T10:46:18Z

## Classes
### `class _GitResult` (line 23)

### `class DesktopGitService` (line 28)
*Минимальный Git-сервис для Desktop UI.*
- `def __init__()` (line 39)
- `async def status_text(session)` (line 55)
- `async def get_commit_context(session)` (line 74)
  - *Контекст для генерации сообщения коммита (status + diff), как в боте.*
- `async def commit(session, message, body)` (line 95)
- `async def pull(session)` (line 168)
  - *Pull from *remote*.*
- `async def push(session)` (line 190)
  - *Push current branch; sets upstream automatically when not configured.*
- `async def fetch(session, remote)` (line 203)
  - *Run ``git fetch --prune <remote>``.*
- `async def merge(session, branch)` (line 207)
  - *Merge *branch* into HEAD.*
- `async def rebase(session, target_branch)` (line 236)
  - *Rebase HEAD onto *target_branch*.*
- `async def log(session, max_count, fmt)` (line 308)
  - *Возвращает историю коммитов в формате, удобном для парсинга.*
- `async def show(session, commit)` (line 337)
  - *Показывает diff конкретного коммита.*
- `async def stash(session)` (line 341)
- `async def stash_pop(session)` (line 344)
- `async def branch_create(session, branch_name)` (line 347)
- `async def checkout(session, branch_name)` (line 350)
