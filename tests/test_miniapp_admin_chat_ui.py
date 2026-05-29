from pathlib import Path


_APP_JS = Path(__file__).resolve().parent.parent / "miniapp" / "static" / "app.js"
_STYLES_CSS = Path(__file__).resolve().parent.parent / "miniapp" / "static" / "styles.css"


def test_admin_chat_layout_ids_present_in_app_js() -> None:
    content = _APP_JS.read_text(encoding="utf-8")
    required_ids = [
        'id="adminChatDetails"',
        'id="adminChatMessages"',
        'id="adminChatInput"',
        'id="adminChatSend"',
        'id="adminChatPendingLabel"',
        'id="adminChatPendingList"',
        'id="adminChatMemory"',
        'id="adminChatMemoryReload"',
        'id="adminChatMemorySave"',
        'id="adminChatStatus"',
    ]
    missing = [item for item in required_ids if item not in content]
    assert not missing, f"Missing admin chat DOM IDs: {missing}"


def test_admin_chat_js_functions_present() -> None:
    content = _APP_JS.read_text(encoding="utf-8")
    required_fns = [
        "function syncAdminChatFromPayload",
        "function renderAdminChatMessages",
        "function renderAdminChatPending",
        "async function fetchAdminChatMessages",
        "async function fetchAdminChatPending",
        "async function fetchAdminChatMemory",
        "async function postAdminChatMessage",
        "async function approveAdminChatPending",
        "async function rejectAdminChatPending",
        "async function saveAdminChatMemory",
        "function bindAdminChatHandlers",
    ]
    missing = [fn for fn in required_fns if fn not in content]
    assert not missing, f"Missing admin chat JS functions: {missing}"


def test_admin_chat_api_endpoints_referenced() -> None:
    content = _APP_JS.read_text(encoding="utf-8")
    required = [
        "/v1/admin/chat/messages",
        "/v1/admin/chat/pending",
        "/v1/admin/chat/memory",
        "/approve",
        "/reject",
    ]
    missing = [item for item in required if item not in content]
    assert not missing, f"Missing admin chat endpoint references: {missing}"


def test_admin_chat_state_fields_present() -> None:
    content = _APP_JS.read_text(encoding="utf-8")
    required = [
        "adminChatCounters",
        "adminChatMessages",
        "adminChatPending",
        "adminChatMemoryDirty",
        "adminChatSendInFlight",
        "adminChatApproveInFlight",
    ]
    missing = [item for item in required if item not in content]
    assert not missing, f"Missing admin chat state fields: {missing}"


def test_admin_chat_sync_invoked_from_render_payload() -> None:
    content = _APP_JS.read_text(encoding="utf-8")
    assert "syncAdminChatFromPayload(payload);" in content


def test_admin_chat_styles_present() -> None:
    content = _STYLES_CSS.read_text(encoding="utf-8")
    required = [
        ".admin-chat-body",
        ".admin-chat-messages",
        ".admin-chat-message",
        ".admin-chat-input-row",
        ".admin-chat-pending-row",
        ".admin-chat-memory-editor",
    ]
    missing = [item for item in required if item not in content]
    assert not missing, f"Missing admin chat CSS selectors: {missing}"


def test_admin_chat_autopilot_indicators_present() -> None:
    js = _APP_JS.read_text(encoding="utf-8")
    css = _STYLES_CSS.read_text(encoding="utf-8")
    required_js = [
        "intent_autopilot_executed",
        "intent_autopilot_blocked",
        "admin-chat-message-autopilot-executed",
        "admin-chat-message-autopilot-blocked",
        "admin-chat-autopilot-blocked",
        "item.autopilot_blocked",
    ]
    missing_js = [item for item in required_js if item not in js]
    assert not missing_js, f"Missing autopilot JS markers: {missing_js}"
    required_css = [
        ".admin-chat-autopilot-blocked",
        ".admin-chat-message-autopilot-executed",
        ".admin-chat-message-autopilot-blocked",
    ]
    missing_css = [item for item in required_css if item not in css]
    assert not missing_css, f"Missing autopilot CSS selectors: {missing_css}"
