import os
import resource
from pathlib import Path

import pytest


def _raise_nofile_soft_limit() -> None:
    try:
        soft_limit, hard_limit = resource.getrlimit(resource.RLIMIT_NOFILE)
    except (OSError, ValueError):
        return
    if hard_limit <= 0 or soft_limit >= hard_limit:
        return
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (hard_limit, hard_limit))
    except (OSError, ValueError):
        return


def _configure_qt_test_env() -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("QT_OPENGL", "software")
    os.environ.setdefault("QT_QUICK_BACKEND", "software")
    os.environ.setdefault("PYTEST_QT_API", "pyside6")

    runtime_dir = Path("/tmp/cli-proxy-pytest-runtime")
    try:
        runtime_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(runtime_dir, 0o700)
    except OSError:
        runtime_dir = Path("/tmp")
    os.environ.setdefault("XDG_RUNTIME_DIR", str(runtime_dir))


_raise_nofile_soft_limit()
_configure_qt_test_env()


@pytest.fixture(scope="session")
def qapp_args() -> list[str]:
    return ["cli-proxy-tests", "-platform", os.environ["QT_QPA_PLATFORM"]]
