from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Iterable, List, Set

from .base import LanguageStack


@dataclass(frozen=True)
class StackMarker:
    stack: LanguageStack
    filenames: Set[str]
    extensions: Set[str]


_MARKERS: List[StackMarker] = [
    StackMarker(
        stack=LanguageStack.PYTHON,
        filenames={"pyproject.toml", "requirements.txt", "setup.py", "setup.cfg", "Pipfile", "poetry.lock"},
        extensions={".py"},
    ),
    StackMarker(
        stack=LanguageStack.TYPESCRIPT,
        filenames={"tsconfig.json"},
        extensions={".ts", ".tsx"},
    ),
    StackMarker(
        stack=LanguageStack.JAVASCRIPT,
        filenames={"package.json", "yarn.lock", "pnpm-lock.yaml", "package-lock.json"},
        extensions={".js", ".jsx", ".mjs", ".cjs"},
    ),
    StackMarker(
        stack=LanguageStack.GO,
        filenames={"go.mod", "go.sum"},
        extensions={".go"},
    ),
    StackMarker(
        stack=LanguageStack.RUST,
        filenames={"Cargo.toml", "Cargo.lock"},
        extensions={".rs"},
    ),
    StackMarker(
        stack=LanguageStack.CPP,
        filenames={"CMakeLists.txt", "meson.build", "Makefile"},
        extensions={".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx", ".h"},
    ),
]


def _iter_project_files(root: str) -> Iterable[tuple[str, str]]:
    for dirpath, _dirnames, filenames in os.walk(root):
        for filename in filenames:
            _, ext = os.path.splitext(filename)
            yield filename, ext.lower()


def detect_stacks(root: str) -> List[LanguageStack]:
    if not root or not os.path.isdir(root):
        return []

    found: Set[LanguageStack] = set()
    for filename, ext in _iter_project_files(root):
        for marker in _MARKERS:
            if filename in marker.filenames or ext in marker.extensions:
                found.add(marker.stack)

    # Keep a stable order for deterministic behavior.
    ordered = [
        LanguageStack.PYTHON,
        LanguageStack.JAVASCRIPT,
        LanguageStack.TYPESCRIPT,
        LanguageStack.GO,
        LanguageStack.RUST,
        LanguageStack.CPP,
    ]
    return [stack for stack in ordered if stack in found]
