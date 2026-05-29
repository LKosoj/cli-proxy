# API Spec: `miniapp/services/files_service.py`

Generated: 2026-04-27T22:43:23Z

## Classes
### `class FilesServiceError(Exception)` (line 20)

### `class SessionUidRequiredError(FilesServiceError)` (line 24)

### `class SessionNotFoundError(FilesServiceError)` (line 28)

### `class PathDeniedError(FilesServiceError)` (line 32)

### `class PathValidationError(FilesServiceError)` (line 36)

### `class RevisionConflictError(FilesServiceError)` (line 40)
- `def __init__(message)` (line 43)

### `class FileTypeError(FilesServiceError)` (line 59)

### `class LocalFilesProvider` (line 82)
*File operations provider that uses a local root directory.*
- `def __init__(root, max_file_size_bytes)` (line 91)
- `def root()` (line 96)
- `def max_file_size_bytes()` (line 100)
- `def tree(resolved_path)` (line 123)
- `def read(resolved_path, rel_path)` (line 186)
- `def write(resolved_path, content, expected_revision)` (line 213)
- `def download(resolved_path)` (line 257)
- `def create(resolved_path, kind)` (line 282)
- `def delete(resolved_path)` (line 297)
- `def meta(resolved_path, rel_path)` (line 306)

### `class RemoteFilesProvider` (line 319)
*File operations provider that works over SSH/SFTP via :class:`SSHService`.*
- `def __init__(ssh_service, workdir, host_alias, remote_root, max_file_size_bytes)` (line 327)
- `def root()` (line 343)
- `def max_file_size_bytes()` (line 347)
- `async def tree(resolved_path)` (line 438)
- `async def read(resolved_path, rel_path)` (line 502)
- `async def write(resolved_path, content, expected_revision)` (line 553)
- `async def download(resolved_path)` (line 619)
- `async def create(resolved_path, kind)` (line 659)
- `async def delete(resolved_path)` (line 686)
- `async def meta(resolved_path, rel_path)` (line 706)

### `class FilesService` (line 730)
*Session-aware file service that selects between Local and Remote providers.*
- `def max_file_size_bytes()` (line 740)
- `def execution_context(session_uid)` (line 812)
  - *Return resolved execution context for *session_uid*.*
- `def tree(user_id, session_uid, rel_path)` (line 882)
- `def read(user_id, session_uid, rel_path)` (line 893)
- `def write(user_id, session_uid, rel_path, content, expected_revision)` (line 904)
- `def download(user_id, session_uid, rel_path)` (line 939)
- `def create(user_id, session_uid, rel_path, kind)` (line 950)
- `def delete(user_id, session_uid, rel_path)` (line 961)
- `def meta(user_id, session_uid, rel_path)` (line 975)
