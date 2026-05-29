from __future__ import annotations

import sqlite3
import threading

from app.bootstrap import build_application
from app.services.scheduled_job_repository import ScheduledJobRepository
from app.services.webhook_delivery_repository import WebhookDeliveryRepository
from config import AppConfig, DefaultsConfig, MCPConfig, MiniAppConfig, SchedulerConfig, TelegramConfig, ToolConfig, WebhooksConfig


class _DummyToolRegistry:
    pass


def _build_config(tmp_path, *, intent: str) -> AppConfig:
    workdir = tmp_path / f"workdir_{intent}"
    runtime = tmp_path / f"runtime_{intent}"
    logs = tmp_path / f"logs_{intent}"
    workdir.mkdir(parents=True, exist_ok=True)
    runtime.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    return AppConfig(
        telegram=TelegramConfig(token="token", whitelist_chat_ids=[1], admlist_chat_ids=[1]),
        tools={
            "dummy": ToolConfig(
                name="dummy",
                mode="headless",
                cmd=["bash", "-lc", "cat"],
            )
        },
        defaults=DefaultsConfig(
            workdir=str(workdir),
            state_path=str(runtime / "state.json"),
            toolhelp_path=str(runtime / "toolhelp.json"),
            log_path=str(logs / "bot.log"),
        ),
        mcp=MCPConfig(enabled=False),
        mcp_clients=[],
        presets=[],
        path=str(tmp_path / f"config_{intent}.yaml"),
        miniapp=MiniAppConfig(),
        webhooks=WebhooksConfig(enabled=True),
        scheduler=SchedulerConfig(enabled=True),
    )


def test_webhook_delivery_repository_claim_is_atomic_and_persists_between_restarts(tmp_path) -> None:
    cfg = _build_config(tmp_path, intent="webhook_dedup")
    barrier = threading.Barrier(2)
    results: list[bool] = []
    results_lock = threading.Lock()

    def _worker() -> None:
        repo = WebhookDeliveryRepository(cfg.defaults.state_path)
        barrier.wait()
        claimed = repo.claim_delivery(
            source="telegram",
            delivery_id="delivery-1",
            payload={"update_id": 101},
        )
        with results_lock:
            results.append(claimed)

    first = threading.Thread(target=_worker)
    second = threading.Thread(target=_worker)
    first.start()
    second.start()
    first.join()
    second.join()

    assert sorted(results) == [False, True]

    restarted = WebhookDeliveryRepository(cfg.defaults.state_path)
    assert restarted.claim_delivery(source="telegram", delivery_id="delivery-1") is False
    restored = restarted.get_delivery(source="telegram", delivery_id="delivery-1")
    assert restored is not None
    assert restored.payload == {"update_id": 101}

    with sqlite3.connect(restarted.db_path) as conn:
        row = conn.execute(
            f"SELECT COUNT(*) FROM {WebhookDeliveryRepository.TABLE_NAME} WHERE source=? AND delivery_id=?",
            ("telegram", "delivery-1"),
        ).fetchone()
    assert row is not None
    assert int(row[0]) == 1


def test_build_application_loads_scheduled_jobs_from_db_on_start_and_isolates_state(tmp_path) -> None:
    cfg_a = _build_config(tmp_path, intent="jobs_a")
    cfg_b = _build_config(tmp_path, intent="jobs_b")

    repo_a = ScheduledJobRepository(cfg_a.defaults.state_path)
    repo_a.upsert_job(
        job_id="job-digest",
        job_name="digest",
        scheduled_for=1700000100.0,
        payload={"intent": "alpha"},
        enabled=True,
    )
    repo_a.upsert_job(
        job_id="job-disabled",
        job_name="disabled",
        scheduled_for=1700000200.0,
        payload={"intent": "skip"},
        enabled=False,
    )

    first_start = build_application(cfg_a, tool_registry_factory=lambda _cfg: _DummyToolRegistry())
    second_start = build_application(cfg_a, tool_registry_factory=lambda _cfg: _DummyToolRegistry())
    isolated_start = build_application(cfg_b, tool_registry_factory=lambda _cfg: _DummyToolRegistry())

    assert [job.job_id for job in first_start.scheduled_jobs] == ["job-digest"]
    assert first_start.scheduled_jobs[0].payload == {"intent": "alpha"}
    assert [job.job_id for job in second_start.scheduled_jobs] == ["job-digest"]
    assert second_start.scheduled_job_repository.get_job("job-digest") is not None
    assert isolated_start.scheduled_jobs == []
