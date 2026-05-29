import types
from pathlib import Path

from modes.webmaster.mode import WebmasterMode
from modes.webmaster.models import WebmasterContext
from modes.webmaster.state_store import WebmasterStateStore, build_user_key


def test_webmaster_store_is_user_scoped(tmp_path):
    store = WebmasterStateStore(str(tmp_path))
    key1 = build_user_key(100, 1, "s1")
    key2 = build_user_key(100, 1, "s2")

    c1 = WebmasterContext(key=key1, goal="task-a", actions=["a1"])
    c2 = WebmasterContext(key=key2, goal="task-b", actions=["b1"])
    store.save(c1)
    store.save(c2)

    r1 = store.load(key1)
    r2 = store.load(key2)
    assert r1.goal == "task-a"
    assert r2.goal == "task-b"
    assert r1.key != r2.key


def test_webmaster_reset_clears_operational_context_only(tmp_path):
    store = WebmasterStateStore(str(tmp_path))
    key = build_user_key(200, 9, "s1")
    ctx = WebmasterContext(
        key=key,
        goal="старый контекст",
        actions=["x"],
        stage="await_feedback",
        last_cli_report="report",
    )
    store.save(ctx)
    learning = {"patches": [{"added_rules": "rule"}], "active_version": 2}
    store.save_prompt_learning(learning)

    reset_ctx = store.reset(key)
    assert reset_ctx.goal == ""
    assert reset_ctx.actions == []
    assert reset_ctx.stage == "idle"

    loaded_learning = store.load_prompt_learning()
    assert loaded_learning["active_version"] == 2
    assert len(loaded_learning["patches"]) == 1


def test_build_user_key_falls_back_to_chat_id_when_user_missing() -> None:
    key = build_user_key(321, None, "s1")
    assert key == "321_321_s1"


def test_webmaster_mode_store_persists_inside_session_workdir(tmp_path) -> None:
    mode = WebmasterMode()
    session = types.SimpleNamespace(workdir=str(tmp_path))
    store = mode._store(session)
    key = build_user_key(11, 22, "s1")

    store.save(WebmasterContext(key=key, goal="task"))

    state_path = Path(store.path_for(key))
    assert state_path == tmp_path / ".cli-proxy" / ".webmaster_data" / "users" / f"{key}.json"
    assert state_path.exists()

    legacy_path = Path(__file__).resolve().parents[1] / "modes" / "webmaster" / "data" / "users" / f"{key}.json"
    assert not legacy_path.exists()


def test_webmaster_prompt_learning_is_normalized_to_general_rules(tmp_path) -> None:
    store = WebmasterStateStore(str(tmp_path))
    store.save_prompt_learning(
        {
            "patches": [
                {
                    "added_rules": [
                        "Для RQ-08 обязательно проверять cancel ветку.",
                        "Всегда требуй проверяемое evidence для каждого пункта чеклиста.",
                    ],
                    "changed_rules": ["Для task_3 добавь проверку rollback."],
                    "removed_rules": [],
                    "reason": "Пропуск по task_3",
                    "expected_effect": "Снижение пропусков по RQ-08",
                }
            ],
            "active_version": 4,
        }
    )

    loaded = store.load_prompt_learning()
    assert loaded["active_version"] == 4
    patches = loaded["patches"]
    assert isinstance(patches, list)
    assert len(patches) == 1
    patch = patches[0]
    assert patch["added_rules"] == ["Всегда требуй проверяемое evidence для каждого пункта чеклиста."]
    assert patch["changed_rules"] == []
    assert patch["reason"] == ""
    assert patch["expected_effect"] == ""
