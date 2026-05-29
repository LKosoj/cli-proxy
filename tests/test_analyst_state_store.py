import json

from modes.analyst.state_store import AnalystContext, AnalystStateStore, build_context_key


def test_analyst_state_store_loads_legacy_json_with_default_new_fields(tmp_path) -> None:
    store = AnalystStateStore(str(tmp_path))
    path = store.path_for("s1")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "mode": "spec",
                "active_flow": "",
                "runtime_template_id": "audit",
                "last_draft": "draft",
                "last_draft_updated_at": 123.0,
                "updated_at": 456.0,
            },
            f,
            ensure_ascii=False,
        )

    ctx = store.load("s1")

    assert ctx.key == "s1"
    assert ctx.runtime_template_id == "audit"
    assert ctx.last_draft == "draft"
    assert ctx.effective_template_id == ""
    assert ctx.intent_reason == ""
    assert ctx.detail_level == ""
    assert ctx.document_kind == ""
    assert ctx.requires_codebase_grounding is False
    assert ctx.requires_repo_audit is False
    assert ctx.requires_final_repo_review is False
    assert ctx.clarification_is_blocking is False
    assert ctx.source_user_text == ""
    assert ctx.clarification_answers == []


def test_analyst_state_store_roundtrip_persists_new_fields(tmp_path) -> None:
    store = AnalystStateStore(str(tmp_path))
    ctx = AnalystContext(
        key="s1",
        effective_template_id="change_spec",
        intent_reason="Запрос явно про ТЗ на доработку",
        detail_level="full",
        document_kind="spec",
        requires_codebase_grounding=True,
        requires_repo_audit=False,
        requires_final_repo_review=True,
        clarification_is_blocking=False,
        source_user_text="Сделай ТЗ",
        clarification_answers=["mobile"],
    )

    store.save(ctx)
    loaded = store.load("s1")

    assert loaded.effective_template_id == "change_spec"
    assert loaded.intent_reason == "Запрос явно про ТЗ на доработку"
    assert loaded.detail_level == "full"
    assert loaded.document_kind == "spec"
    assert loaded.requires_codebase_grounding is True
    assert loaded.requires_repo_audit is False
    assert loaded.requires_final_repo_review is True
    assert loaded.clarification_is_blocking is False
    assert loaded.source_user_text == "Сделай ТЗ"
    assert loaded.clarification_answers == ["mobile"]


def test_build_context_key_uses_chat_id_and_session_id() -> None:
    assert build_context_key(42, "s1") == "42_s1"
    assert build_context_key("chat/A", "sess\\B") == "chat_A_sess_B"


def test_analyst_state_store_isolated_by_chat_for_same_session_id(tmp_path) -> None:
    store = AnalystStateStore(str(tmp_path))
    key_chat1 = build_context_key(1, "s-shared")
    key_chat2 = build_context_key(2, "s-shared")

    ctx1 = store.load(key_chat1)
    ctx1.runtime_template_id = "audit"
    ctx1.intent_reason = "intent-1"
    store.save(ctx1)

    ctx2 = store.load(key_chat2)
    assert ctx2.runtime_template_id == ""
    assert ctx2.intent_reason == ""

    ctx2.runtime_template_id = "default"
    ctx2.intent_reason = "intent-2"
    store.save(ctx2)

    reloaded1 = store.load(key_chat1)
    reloaded2 = store.load(key_chat2)
    assert reloaded1.runtime_template_id == "audit"
    assert reloaded1.intent_reason == "intent-1"
    assert reloaded2.runtime_template_id == "default"
    assert reloaded2.intent_reason == "intent-2"


def test_analyst_state_store_does_not_read_unscoped_session_key_for_composite_context(tmp_path) -> None:
    store = AnalystStateStore(str(tmp_path))
    legacy_key = "s1"
    with open(store.path_for(legacy_key), "w", encoding="utf-8") as f:
        json.dump({"runtime_template_id": "audit", "intent_reason": "legacy"}, f, ensure_ascii=False)

    key_chat1 = build_context_key(1, legacy_key)
    key_chat2 = build_context_key(2, legacy_key)

    ctx_chat1 = store.load(key_chat1)
    assert ctx_chat1.runtime_template_id == ""
    assert ctx_chat1.intent_reason == ""

    ctx_chat2 = store.load(key_chat2)
    assert ctx_chat2.runtime_template_id == ""
    assert ctx_chat2.intent_reason == ""
