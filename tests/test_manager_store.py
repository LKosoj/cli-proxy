import threading

import pytest

from modes.sdk.runtime.contracts import DevTask, ProjectAnalysis, ProjectPlan
from modes.sdk.planning import archive_plan, delete_plan, load_plan, manager_plan_path, save_plan
import modes.sdk.planning as planning


def _build_plan(*, goal: str, attempt: int) -> ProjectPlan:
    return ProjectPlan(
        project_goal=goal,
        analysis=ProjectAnalysis(current_state="cs", already_done=["a"], remaining_work=["b"]),
        tasks=[
            DevTask(
                id="task_1",
                title="t1",
                description="d1",
                acceptance_criteria=["c1", "c2"],
                depends_on=[],
                status="pending",
                attempt=attempt,
                max_attempts=3,
                dev_report="r",
            )
        ],
        status="active",
        created_at="",
        updated_at="",
    )


def test_manager_store_roundtrip(tmp_path):
    wd = str(tmp_path)
    plan = _build_plan(goal="goal", attempt=1)
    save_plan(wd, plan)
    loaded = load_plan(wd)
    assert loaded is not None
    assert loaded.project_goal == "goal"
    assert loaded.analysis is not None
    assert loaded.analysis.current_state == "cs"
    assert len(loaded.tasks) == 1
    assert loaded.tasks[0].id == "task_1"
    assert loaded.tasks[0].attempt == 1

    archived = archive_plan(wd, status="active")
    assert archived is not None
    assert "/.cli-proxy/.manager_archive/" in archived.replace("\\", "/")
    # After archiving, plan should be gone.
    assert load_plan(wd) is None

    # delete is idempotent
    delete_plan(wd)
    delete_plan(wd)


def test_manager_store_uses_fs_lock_for_load_save_archive(monkeypatch, tmp_path):
    wd = str(tmp_path)
    lock_calls = []
    real_lock_file = planning.lock_file

    def _spy_lock_file(fh, *, shared):
        lock_calls.append(bool(shared))
        return real_lock_file(fh, shared=shared)

    monkeypatch.setattr(planning, "lock_file", _spy_lock_file)

    save_plan(wd, _build_plan(goal="goal", attempt=1))
    assert load_plan(wd) is not None
    assert archive_plan(wd, status="active") is not None

    assert True in lock_calls
    assert lock_calls.count(False) >= 2


def test_manager_store_keeps_lock_file_in_cli_proxy_manager_dir(tmp_path):
    wd = str(tmp_path)
    save_plan(wd, _build_plan(goal="goal", attempt=1))

    lock_path = tmp_path / ".cli-proxy" / ".manager" / "MANAGER_PLAN.json.lock"
    legacy_lock_path = tmp_path / "MANAGER_PLAN.json.lock"
    assert lock_path.exists()
    assert not legacy_lock_path.exists()


def test_manager_store_concurrent_save_load_without_json_corruption(tmp_path):
    wd = str(tmp_path)
    save_plan(wd, _build_plan(goal="seed", attempt=0))
    failures = []

    def _writer(worker_id: int) -> None:
        for idx in range(120):
            try:
                attempt = worker_id * 1000 + idx
                save_plan(wd, _build_plan(goal=f"goal-{attempt}", attempt=attempt))
            except Exception as exc:  # pragma: no cover - assert below
                failures.append(f"writer:{worker_id}:{exc}")

    def _reader() -> None:
        for _ in range(500):
            try:
                plan = load_plan(wd)
                if plan is None:
                    failures.append("reader:none")
                    continue
                if not plan.tasks:
                    failures.append("reader:no_tasks")
                    continue
                _ = int(plan.tasks[0].attempt)
            except Exception as exc:  # pragma: no cover - assert below
                failures.append(f"reader:{exc}")

    threads = [threading.Thread(target=_reader) for _ in range(3)]
    threads.extend(threading.Thread(target=_writer, args=(wid,)) for wid in range(4))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not failures
    final_plan = load_plan(wd)
    assert final_plan is not None
    assert final_plan.tasks


def test_manager_store_persists_manager_max_tasks_limit_runtime_hint(tmp_path):
    wd = str(tmp_path)
    plan = _build_plan(goal="goal", attempt=1)
    setattr(plan, "_manager_max_tasks_limit", 17)

    save_plan(wd, plan)
    loaded = load_plan(wd)

    assert loaded is not None
    assert int(getattr(loaded, "_manager_max_tasks_limit", 0) or 0) == 17


def test_manager_store_raises_when_save_fails(monkeypatch, tmp_path):
    wd = str(tmp_path)
    plan = _build_plan(goal="goal", attempt=1)

    def _boom(_path, _data):
        raise RuntimeError("disk full")

    monkeypatch.setattr(planning, "write_json_locked", _boom)

    with pytest.raises(RuntimeError, match="disk full"):
        save_plan(wd, plan)


def test_manager_store_scoped_plan_path_roundtrip(tmp_path):
    wd = str(tmp_path)
    scoped_a = "1_s1"
    scoped_b = "2_s1"
    plan_a = _build_plan(goal="goal-a", attempt=1)
    plan_b = _build_plan(goal="goal-b", attempt=2)

    save_plan(wd, plan_a, scoped_key=scoped_a)
    save_plan(wd, plan_b, scoped_key=scoped_b)

    path_a = manager_plan_path(wd, scoped_key=scoped_a)
    path_b = manager_plan_path(wd, scoped_key=scoped_b)

    assert path_a.endswith("plan_1_s1.json")
    assert path_b.endswith("plan_2_s1.json")
    assert path_a != path_b
    loaded_a = load_plan(wd, scoped_key=scoped_a)
    loaded_b = load_plan(wd, scoped_key=scoped_b)
    assert loaded_a is not None
    assert loaded_b is not None
    assert loaded_a.project_goal == "goal-a"
    assert loaded_b.project_goal == "goal-b"


def test_manager_store_migrates_legacy_global_plan_to_scoped_file_on_first_access(tmp_path, caplog):
    wd = str(tmp_path)
    legacy_plan = _build_plan(goal="legacy-goal", attempt=3)
    save_plan(wd, legacy_plan)

    caplog.set_level("WARNING")
    scoped_key = "1_s1"
    migrated = load_plan(wd, scoped_key=scoped_key)

    assert migrated is not None
    assert migrated.project_goal == "legacy-goal"
    assert not (tmp_path / "MANAGER_PLAN.json").exists()
    assert (tmp_path / ".cli-proxy" / ".manager" / "plans" / "plan_1_s1.json").exists()
    assert any("migrated legacy manager plan to scoped storage" in rec.message for rec in caplog.records)
