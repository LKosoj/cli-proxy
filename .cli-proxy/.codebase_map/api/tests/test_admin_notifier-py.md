# API Spec: `tests/test_admin_notifier.py`

Generated: 2026-04-27T22:43:22Z

## Classes
### `class _FakeMessaging` (line 50)
- `def __init__()` (line 51)
- `async def send_text(chat_id, text)` (line 54)

## Symbols
- `def test_admin_notifier_does_not_send_notifications_during_mute_period(tmp_path)` (line 66)
- `def test_admin_notifier_formats_incident_and_action_messages_as_markdownv2(tmp_path)` (line 95)
- `def test_admin_notifier_state_isolated_between_sequential_sessions(tmp_path)` (line 158)
