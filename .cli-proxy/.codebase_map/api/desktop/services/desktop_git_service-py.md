# API Spec: `desktop/services/desktop_git_service.py`

Generated: 2026-04-27T22:43:22Z

## Classes
### `class _GitResult` (line 19)

### `class DesktopGitService` (line 24)
*Минимальный Git-сервис для Desktop UI.*
- `def __init__()` (line 33)
- `async def status_text(session)` (line 46)
- `async def get_commit_context(session)` (line 65)
  - *Контекст для генерации сообщения коммита (status + diff), как в боте.*
- `async def commit(session, message, body)` (line 86)
- `async def pull(session)` (line 98)
- `async def push(session)` (line 101)
- `async def log(session, max_count, fmt)` (line 104)
  - *Возвращает историю коммитов в формате, удобном для парсинга.*
- `async def show(session, commit)` (line 133)
  - *Показывает diff конкретного коммита.*
- `async def stash(session)` (line 137)
- `async def stash_pop(session)` (line 140)
- `async def branch_create(session, branch_name)` (line 143)
- `async def checkout(session, branch_name)` (line 146)
