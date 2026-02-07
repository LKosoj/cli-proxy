from agent.contracts import DevTask, ProjectAnalysis, ProjectPlan
from agent.manager_store import load_plan, save_plan, delete_plan, archive_plan


def test_manager_store_roundtrip(tmp_path):
    wd = str(tmp_path)
    plan = ProjectPlan(
        project_goal="goal",
        analysis=ProjectAnalysis(current_state="cs", already_done=["a"], remaining_work=["b"]),
        tasks=[
            DevTask(
                id="task_1",
                title="t1",
                description="d1",
                acceptance_criteria=["c1", "c2"],
                depends_on=[],
                status="pending",
                attempt=1,
                max_attempts=3,
                dev_report="r",
            )
        ],
        status="active",
        created_at="",
        updated_at="",
    )
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
    # After archiving, plan should be gone.
    assert load_plan(wd) is None

    # delete is idempotent
    delete_plan(wd)
    delete_plan(wd)

