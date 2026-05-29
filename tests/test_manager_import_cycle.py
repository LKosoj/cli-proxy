from __future__ import annotations

import subprocess
import sys


def test_import_agent_manager_without_circular_import() -> None:
    # Run in a fresh interpreter to avoid mutating sys.modules in current test process.
    proc = subprocess.run(
        [sys.executable, "-c", "import agent.manager"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
