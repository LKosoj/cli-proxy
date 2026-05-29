from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys
import textwrap

import pytest


def _write_executable(path: pathlib.Path, content: str) -> pathlib.Path:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)
    return path


def _prepare_repo_copy(tmp_path: pathlib.Path) -> pathlib.Path:
    repo_root = pathlib.Path(__file__).resolve().parents[2]
    copy_root = tmp_path / "repo"
    copy_root.mkdir(parents=True, exist_ok=True)
    for name in ("setup_bot.sh", "config_example.yaml", "requirements.txt"):
        shutil.copy2(repo_root / name, copy_root / name)
    return copy_root


def _prepare_fake_bin(tmp_path: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir(parents=True, exist_ok=True)
    log_path = tmp_path / "command-log.txt"
    service_path = tmp_path / "cli-proxy-bot.service"
    real_python3 = sys.executable

    simple_logger = textwrap.dedent(
        """\
        #!/usr/bin/env bash
        set -euo pipefail
        printf '%s\\n' "$(basename "$0") $*" >> "${FAKE_LOG_PATH}"
        exit 0
        """
    )
    for command in ("apt-get", "curl", "npm", "systemctl", "chown", "id"):
        _write_executable(fakebin / command, simple_logger)

    _write_executable(
        fakebin / "tee",
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            set -euo pipefail
            printf '%s\\n' "$(basename "$0") $*" >> "${FAKE_LOG_PATH}"
            cat > "${FAKE_SERVICE_PATH}"
            """
        ),
    )
    _write_executable(
        fakebin / "sudo",
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            set -euo pipefail
            printf '%s\\n' "$(basename "$0") $*" >> "${FAKE_LOG_PATH}"
            exec "$@"
            """
        ),
    )
    _write_executable(
        fakebin / "python3",
        textwrap.dedent(
            f"""\
            #!/usr/bin/env bash
            set -euo pipefail
            printf '%s\\n' "$(basename "$0") $*" >> "${{FAKE_LOG_PATH}}"
            if [[ "${{1:-}}" == "-m" && "${{2:-}}" == "venv" ]]; then
              venv_dir="${{3:?venv_dir}}"
              mkdir -p "$venv_dir/bin"
              cat > "$venv_dir/bin/python" <<'SH'
            #!/usr/bin/env bash
            set -euo pipefail
            if [[ "${{1:-}}" == "-m" && "${{2:-}}" == "pip" ]]; then
              exit 0
            fi
            exec "{real_python3}" "$@"
            SH
              chmod 755 "$venv_dir/bin/python"
              cat > "$venv_dir/bin/pip" <<'SH'
            #!/usr/bin/env bash
            set -euo pipefail
            exit 0
            SH
              chmod 755 "$venv_dir/bin/pip"
              exit 0
            fi
            exec "{real_python3}" "$@"
            """
        ),
    )

    return log_path, service_path


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required for setup_bot.sh smoke")
def test_setup_bot_script_non_interactive_smoke_uses_stubbed_commands(tmp_path) -> None:
    repo_root = _prepare_repo_copy(tmp_path)
    log_path, service_path = _prepare_fake_bin(tmp_path)
    fakebin = tmp_path / "fakebin"
    workdir = tmp_path / "projects"

    env = dict(os.environ)
    env["PATH"] = f"{fakebin}{os.pathsep}{env.get('PATH', '')}"
    env["FAKE_LOG_PATH"] = str(log_path)
    env["FAKE_SERVICE_PATH"] = str(service_path)
    env["OPENAI_API_KEY"] = "test-key"
    env["OPENAI_MODEL"] = "gpt-4.1-mini"
    env["OPENAI_BIG_MODEL"] = "gpt-4.1"

    completed = subprocess.run(
        [
            shutil.which("bash") or "bash",
            "setup_bot.sh",
            "--non-interactive",
            "--bot-token",
            "123:token",
            "--whitelist",
            "1",
            "--admins",
            "1",
            "--workdir",
            str(workdir),
            "--service-name",
            "cli-proxy-bot",
            "--service-user",
            "runner",
            "--chown",
            "no",
        ],
        cwd=repo_root,
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert (repo_root / "config.yaml").exists()
    assert (repo_root / ".env").exists()
    assert "GEMINI_OAUTH_CLIENT_SECRET" not in (repo_root / ".env").read_text(encoding="utf-8")
    assert service_path.exists()
    assert "Setup completed" in completed.stdout

    log_lines = log_path.read_text(encoding="utf-8").splitlines()
    assert any(line.startswith("apt-get update -y") for line in log_lines)
    assert any(line.startswith("apt-get install -y") for line in log_lines)
    assert any(line.startswith("systemctl daemon-reload") for line in log_lines)
    assert any(line.startswith("systemctl enable --now cli-proxy-bot") for line in log_lines)
