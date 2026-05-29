from pathlib import Path

import yaml


def test_ci_workflow_has_cross_platform_matrix_and_quality_gates() -> None:
    workflow_path = Path(".github/workflows/ci.yml")
    assert workflow_path.exists(), "CI workflow file is missing"

    data = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    on = data.get("on") or data.get(True) or {}
    assert "push" in on
    assert "pull_request" in on

    jobs = data.get("jobs") or {}
    assert "quality-gates" in jobs
    job = jobs["quality-gates"]

    matrix = (((job.get("strategy") or {}).get("matrix")) or {})
    os_values = set(matrix.get("os") or [])
    assert {"ubuntu-latest", "windows-latest", "macos-latest"} <= os_values
    assert matrix.get("python-version") == ["3.12"]

    steps = job.get("steps") or []
    joined = "\n".join(str(step.get("run") or "") for step in steps)
    assert "--cov-fail-under=55" in joined

    step_names = [str(step.get("name") or "") for step in steps]
    assert "Unit/UI tests + coverage gate" in step_names
    assert "Build source artifact" in step_names
    assert "Smoke tests" in step_names
    assert step_names.index("Unit/UI tests + coverage gate") < step_names.index("Build source artifact")
    assert step_names.index("Build source artifact") < step_names.index("Smoke tests")

    lint_steps = [s for s in steps if s.get("name") == "Lint"]
    assert len(lint_steps) == 1
    lint_step = lint_steps[0]
    assert lint_step.get("shell") == "python"
    lint_script = str(lint_step.get("run") or "")
    assert "git\", \"ls-files\", \"*.py\"" in lint_script
    assert "flake8" in lint_script
    assert "flake8 ." not in lint_script
    assert ".github/workflows/ci.yml" not in lint_script

    build_steps = [s for s in steps if s.get("name") == "Build source artifact"]
    assert len(build_steps) == 1
    build_step = build_steps[0]
    build_script = str(build_step.get("run") or "")
    assert build_step.get("shell") == "bash"
    assert "python -m utils.source_artifact build --root ." in build_script
    assert "reports/source_artifact.json" in build_script

    smoke_steps = [s for s in steps if s.get("name") == "Smoke tests"]
    assert len(smoke_steps) == 1
    smoke_step = smoke_steps[0]
    smoke_script = str(smoke_step.get("run") or "")
    assert smoke_step.get("shell") == "bash"
    assert "pytest -q tests/smoke" in smoke_script
    smoke_env = smoke_step.get("env") or {}
    assert smoke_env.get("SMOKE_SOURCE_ARTIFACT") == "dist/source-*.zip"

    unit_steps = [s for s in steps if s.get("name") == "Unit/UI tests + coverage gate"]
    assert len(unit_steps) == 1
    assert unit_steps[0].get("shell") == "bash"

    upload_steps = [
        s for s in steps if str(s.get("uses") or "").startswith("actions/upload-artifact@")
    ]
    assert len(upload_steps) >= 2
