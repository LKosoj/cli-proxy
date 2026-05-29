from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
AUDIT_DOC = ROOT / "docs" / "exception-handling-audit.md"
MANDATORY_FILES = {
    "sessions/session_run_service.py",
    "sessions/session_output_service.py",
    "modes/admin/transports/local.py",
}
REPO_SCOPE_DIRS = (
    "app",
    "modes",
    "sessions",
    "tg",
    "desktop",
    "miniapp",
)
REPO_SCOPE_FILES = (
    "session.py",
    "bot.py",
)
SILENT_EXCEPT_PASS_RE = re.compile(
    r"(?m)^(?P<indent>[ \t]*)except Exception:\n(?P=indent)[ \t]+pass\b"
)
AUDIT_ENTRY_RE = re.compile(
    r"^\| `(?P<path>[^`:]+\.py):(?P<line>\d+)` \| "
    r"(?P<context>.*?) \| "
    r"`(?P<category>best_effort_cleanup|legacy_fallback|bug)` \|",
    re.MULTILINE,
)


@dataclass(frozen=True, order=True)
class SilentExceptMatch:
    path: str
    line: int

    def label(self) -> str:
        return f"{self.path}:{self.line}"


def _iter_repo_scope_files() -> list[Path]:
    paths: list[Path] = []
    for dirname in REPO_SCOPE_DIRS:
        paths.extend(sorted((ROOT / dirname).rglob("*.py")))
    for filename in REPO_SCOPE_FILES:
        paths.append(ROOT / filename)
    return paths


def _find_silent_except_pass(paths: list[Path]) -> set[SilentExceptMatch]:
    matches: set[SilentExceptMatch] = set()
    for path in paths:
        text = path.read_text(encoding="utf-8")
        relative_path = path.relative_to(ROOT).as_posix()
        matches.update(_find_silent_except_pass_in_text(relative_path, text))
    return matches


def _find_silent_except_pass_in_text(relative_path: str, text: str) -> set[SilentExceptMatch]:
    return {
        SilentExceptMatch(relative_path, text.count("\n", 0, match.start()) + 1)
        for match in SILENT_EXCEPT_PASS_RE.finditer(text)
    }


def _is_generic_exception_handler(handler_type: ast.expr | None) -> bool:
    if handler_type is None:
        return True
    if isinstance(handler_type, ast.Name):
        return handler_type.id in {"Exception", "BaseException"}
    if isinstance(handler_type, ast.Attribute):
        return handler_type.attr in {"Exception", "BaseException"}
    if isinstance(handler_type, ast.Tuple):
        return any(_is_generic_exception_handler(elt) for elt in handler_type.elts)
    return False


def _find_generic_exception_pass_handlers(paths: list[Path]) -> set[SilentExceptMatch]:
    matches: set[SilentExceptMatch] = set()
    for path in paths:
        text = path.read_text(encoding="utf-8")
        relative_path = path.relative_to(ROOT).as_posix()
        tree = ast.parse(text, filename=relative_path)
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            if not _is_generic_exception_handler(node.type):
                continue
            if any(isinstance(statement, ast.Pass) for statement in node.body):
                matches.add(SilentExceptMatch(relative_path, node.lineno))
    return matches


def _audit_allowlist() -> set[SilentExceptMatch]:
    text = AUDIT_DOC.read_text(encoding="utf-8")
    _, classification = text.split("## Exact Grep Classification", 1)
    classification, _ = classification.split("## Summary", 1)
    allowed = {
        SilentExceptMatch(match.group("path"), int(match.group("line")))
        for match in AUDIT_ENTRY_RE.finditer(classification)
        if match.group("path") not in MANDATORY_FILES
    }
    assert all(match.path not in MANDATORY_FILES for match in allowed)
    return allowed


def test_silent_exception_pass_detector_flags_new_mandatory_violation() -> None:
    text = "\n".join(
        [
            "async def run():",
            "    try:",
            "        await work()",
            "    except Exception:",
            "        pass",
            "",
        ]
    )

    assert _find_silent_except_pass_in_text("sessions/session_run_service.py", text) == {
        SilentExceptMatch("sessions/session_run_service.py", 4)
    }


def test_mandatory_first_batch_has_no_silent_exception_pass() -> None:
    paths = [ROOT / relative_path for relative_path in sorted(MANDATORY_FILES)]
    matches = _find_silent_except_pass(paths)

    assert not matches, (
        "Mandatory first-batch files must stay zero-match for exact silent fallback pattern "
        f"`except Exception:` followed by `pass`: {[match.label() for match in sorted(matches)]}"
    )


def test_mandatory_first_batch_has_no_generic_exception_pass_handler() -> None:
    paths = [ROOT / relative_path for relative_path in sorted(MANDATORY_FILES)]
    matches = _find_generic_exception_pass_handlers(paths)

    assert not matches, (
        "Mandatory first-batch files must not contain generic exception handlers "
        f"with direct pass: {[match.label() for match in sorted(matches)]}"
    )


def test_repo_wide_silent_exception_pass_matches_are_documented() -> None:
    matches = _find_silent_except_pass(_iter_repo_scope_files())
    mandatory_matches = {match for match in matches if match.path in MANDATORY_FILES}
    allowed = _audit_allowlist()
    unexpected = sorted(matches - allowed - mandatory_matches)

    assert not mandatory_matches, (
        "Audit allowlist must not hide mandatory first-batch files: "
        f"{[match.label() for match in sorted(mandatory_matches)]}"
    )
    assert not unexpected, (
        "Repo-wide silent `except Exception: pass` matches must be classified in "
        f"{AUDIT_DOC.relative_to(ROOT)}: {[match.label() for match in unexpected]}"
    )
