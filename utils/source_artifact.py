from __future__ import annotations

import argparse
import itertools
import json
import logging
import os
import pathlib
import zipfile
from typing import Iterable, Sequence

logger = logging.getLogger(__name__)

_RGLOB_FILES_LIMIT = 5_000


SOURCE_ARTIFACT_INCLUDE: tuple[str, ...] = (
    "app",
    "agent",
    "miniapp",
    "modes",
    "sessions",
    "tg",
    "tests",
    "utils",
    "bot.py",
    "config.py",
    "config_example.yaml",
    "README.md",
    "README_EN.MD",
    "requirements.txt",
    "session.py",
    "setup_bot.sh",
    "summary.py",
    "pytest.ini",
)

REQUIRED_ARTIFACT_MEMBERS: tuple[str, ...] = (
    "app/bootstrap.py",
    "bot.py",
    "config.py",
    "config_example.yaml",
    "miniapp/static/index.html",
    "README.md",
    "README_EN.MD",
    "requirements.txt",
    "session.py",
    "setup_bot.sh",
    "summary.py",
    "tests/smoke/test_bot_entrypoint_smoke.py",
    "tests/smoke/test_miniapp_server_smoke.py",
    "tests/smoke/test_setup_bot_script.py",
    "utils/paths.py",
)

FORBIDDEN_ARTIFACT_MEMBERS: tuple[str, ...] = (
    "config.yaml",
    ".env",
)

_NOISE_DIR_NAMES = {
    "__pycache__",
    ".git",
    ".mypy_cache",
    ".playwright-cli",
    ".pytest_cache",
    ".venv",
    "dist",
    "logs",
    "session_ticks",
}
_NOISE_SUFFIXES = (".log", ".pyc", ".pyo", ".sqlite3", ".sqlite3-shm", ".sqlite3-wal")


def _runner_os_slug(value: str | None = None) -> str:
    raw = str(value or os.environ.get("RUNNER_OS") or os.name or "unknown").strip().lower()
    mapping = {
        "linux": "linux",
        "ubuntu": "linux",
        "posix": "linux",
        "darwin": "macos",
        "macos": "macos",
        "windows": "windows",
        "nt": "windows",
    }
    return mapping.get(raw, raw.replace(" ", "-"))


def default_output_path(
    *,
    root: pathlib.Path | str = ".",
    output_dir: pathlib.Path | str = "dist",
    runner_os: str | None = None,
) -> pathlib.Path:
    root_path = pathlib.Path(root).resolve()
    out_dir = (root_path / output_dir).resolve()
    return out_dir / f"source-{_runner_os_slug(runner_os)}.zip"


def _is_noise_relative(path: pathlib.PurePosixPath) -> bool:
    if any(part in _NOISE_DIR_NAMES for part in path.parts[:-1]):
        return True
    return path.name.endswith(_NOISE_SUFFIXES)


def iter_source_artifact_files(
    *,
    root: pathlib.Path | str = ".",
    include: Sequence[str] = SOURCE_ARTIFACT_INCLUDE,
) -> Iterable[pathlib.Path]:
    root_path = pathlib.Path(root).resolve()
    seen: set[str] = set()
    for item in include:
        candidate = (root_path / item).resolve()
        if not candidate.exists():
            continue
        if candidate.is_file():
            arcname = candidate.relative_to(root_path).as_posix()
            if arcname in seen:
                continue
            seen.add(arcname)
            yield candidate
            continue
        if not candidate.is_dir():
            continue
        raw_files = list(itertools.islice((p for p in candidate.rglob("*") if p.is_file()), _RGLOB_FILES_LIMIT))
        if len(raw_files) >= _RGLOB_FILES_LIMIT:
            logger.warning(
                "source_artifact: rglob scan truncated at %d files under %s",
                _RGLOB_FILES_LIMIT,
                candidate,
            )
        for file_path in sorted(raw_files):
            arcname = file_path.relative_to(root_path).as_posix()
            if _is_noise_relative(pathlib.PurePosixPath(arcname)):
                continue
            if arcname in seen:
                continue
            seen.add(arcname)
            yield file_path


def build_source_artifact(
    *,
    root: pathlib.Path | str = ".",
    output: pathlib.Path | str | None = None,
    include: Sequence[str] = SOURCE_ARTIFACT_INCLUDE,
) -> pathlib.Path:
    root_path = pathlib.Path(root).resolve()
    output_path = pathlib.Path(output).resolve() if output is not None else default_output_path(root=root_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for file_path in iter_source_artifact_files(root=root_path, include=include):
            archive.write(file_path, arcname=file_path.relative_to(root_path).as_posix())
    return output_path


def inspect_source_artifact(artifact_path: pathlib.Path | str) -> dict[str, object]:
    resolved = pathlib.Path(artifact_path).resolve()
    with zipfile.ZipFile(resolved) as archive:
        members = sorted(
            info.filename
            for info in archive.infolist()
            if not info.is_dir()
        )
    return {
        "artifact_path": str(resolved),
        "members": members,
        "member_count": len(members),
    }


def validate_source_artifact(
    artifact_path: pathlib.Path | str,
    *,
    required_members: Sequence[str] = REQUIRED_ARTIFACT_MEMBERS,
    forbidden_members: Sequence[str] = FORBIDDEN_ARTIFACT_MEMBERS,
) -> dict[str, object]:
    report = inspect_source_artifact(artifact_path)
    members = set(str(item) for item in report["members"])
    missing = sorted(member for member in required_members if member not in members)
    forbidden_present = sorted(member for member in forbidden_members if member in members)
    noisy_members = sorted(
        member
        for member in members
        if _is_noise_relative(pathlib.PurePosixPath(member))
    )
    if missing or forbidden_present or noisy_members:
        raise ValueError(
            json.dumps(
                {
                    "artifact_path": report["artifact_path"],
                    "missing_required": missing,
                    "forbidden_present": forbidden_present,
                    "noisy_members": noisy_members,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    report["required_members"] = list(required_members)
    report["forbidden_members"] = list(forbidden_members)
    return report


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build and validate cli-proxy source artifacts.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    build_parser = subparsers.add_parser("build", help="Build source artifact zip.")
    build_parser.add_argument("--root", default=".", help="Repository root path.")
    build_parser.add_argument("--output", default=None, help="Destination artifact path.")

    validate_parser = subparsers.add_parser("validate", help="Validate an existing source artifact zip.")
    validate_parser.add_argument("artifact", help="Artifact path to validate.")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "build":
        artifact_path = build_source_artifact(root=args.root, output=args.output)
        report = validate_source_artifact(artifact_path)
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0
    if args.command == "validate":
        report = validate_source_artifact(args.artifact)
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0
    parser.error(f"unsupported command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
