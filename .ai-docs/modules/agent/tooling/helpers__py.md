# agent/tooling/helpers

```markdown
This module provides secure execution of shell commands and file operations within a restricted workspace environment. It includes mechanisms for command approval, pattern-based blocking, workspace isolation, and output sanitization to prevent security risks. The module also handles background process launching and enforces timeouts to avoid long-running or hanging operations.

Ключевые структуры данных
PendingCommand — Represents a shell command awaiting user approval, storing metadata such as session, chat, command, and reason

set_approval_callback(cb: Callable[[int, str, str, str], None]) -> None
Sets a callback function to be invoked when a command requires user approval.
Аргументы
cb — Callback function accepting chat_id, session_id, cmd_id, and reason
Возвращает
None

---
_store_pending_command(session_id: str, chat_id: int, command: str, cwd: str, reason: str) -> str
Stores a new pending command and returns its unique identifier.
Аргументы
session_id — Identifier of the current session
chat_id — Identifier of the chat initiating the command
command — Shell command to be executed
cwd — Current working directory for command execution
reason — Explanation for why approval is needed
Возвращает
Unique command ID for later retrieval or approval

---
pop_pending_command(cmd_id: str) -> Optional[PendingCommand]
Removes and returns a pending command by its ID, if it exists.
Аргументы
cmd_id — Unique identifier of the pending command
Возвращает
The PendingCommand object if found, otherwise None

---
_load_blocked_patterns() -> List[Dict[str, Any]]
Loads and parses blocked command patterns from the configuration file.
Возвращает
List of pattern dictionaries containing regex, category, and block status

---
check_command(command: str, chat_type: Optional[str]) -> Tuple[bool, bool, Optional[str]]
Checks if a command matches any blocked or approved patterns.
Аргументы
command — Command string to evaluate
chat_type — Type of chat (e.g. "group") for context-aware filtering
Возвращает
Tuple indicating (is_approved, is_blocked, reason)

---
_check_workspace_isolation(command: str, user_workspace: str) -> Tuple[bool, Optional[str]]
Verifies that a command does not access system-critical directories.
Аргументы
command — Command to check for dangerous paths
user_workspace — Root directory allowed for user operations
Возвращает
Tuple indicating (is_violating, error_message)

---
_check_command_path_escape(command: str, cwd: str) -> Tuple[bool, Optional[str]]
Ensures all absolute paths in the command stay within the workspace.
Аргументы
command — Command to validate
cwd — Current working directory as workspace root
Возвращает
Tuple indicating (escapes_workspace, error_message)

---
sanitize_output(output: str) -> str
Removes ANSI escape codes from command output.
Аргументы
output — Raw command output string
Возвращает
Cleaned string safe for display

---
_trim_output(text: str) -> str
Truncates long output by keeping head and tail portions.
Аргументы
text — Full output text
Возвращает
Trimmed version with truncation indicator

---
execute_shell_command(command: str, cwd: str) -> Dict[str, Any]
Executes a shell command with timeout and returns structured result.
Аргументы
command — Command to execute
cwd — Working directory for execution
Возвращает
Dictionary with 'success', 'output', or 'error' keys
Исключения
subprocess.TimeoutExpired — Raised when execution exceeds timeout

---
_resolve_within_workspace(path: str, cwd: str) -> Tuple[Optional[str], Optional[str]]
Resolves a path and verifies it stays within the allowed workspace.
Аргументы
path — Input file path (relative or absolute)
cwd — Base directory for resolving relative paths
Возвращает
Tuple of (resolved_path, error_message)
```
