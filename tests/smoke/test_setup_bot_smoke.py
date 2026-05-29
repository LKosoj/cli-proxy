from __future__ import annotations

import os
import pathlib
import shutil
import subprocess

import pytest


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required for setup_bot.sh smoke")
def test_setup_bot_help_smoke_avoids_install_commands(tmp_path) -> None:
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir(parents=True, exist_ok=True)
    markers_dir = tmp_path / "markers"
    markers_dir.mkdir(parents=True, exist_ok=True)

    for command in ("apt-get", "curl", "npm", "sudo", "systemctl"):
        marker = markers_dir / command
        wrapper = fakebin / command
        wrapper.write_text(
            "\n".join(
                [
                    "#!/usr/bin/env bash",
                    "set -euo pipefail",
                    f"touch {marker!s}",
                    "echo \"unexpected command invocation\" >&2",
                    "exit 99",
                ]
            ),
            encoding="utf-8",
        )
        wrapper.chmod(0o755)

    env = dict(os.environ)
    env["PATH"] = f"{fakebin}{os.pathsep}{env.get('PATH', '')}"

    completed = subprocess.run(
        [shutil.which("bash") or "bash", "setup_bot.sh", "--help"],
        cwd=pathlib.Path(__file__).resolve().parents[2],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "Usage:" in completed.stdout
    assert "--non-interactive" in completed.stdout
    assert not any(markers_dir.iterdir())
