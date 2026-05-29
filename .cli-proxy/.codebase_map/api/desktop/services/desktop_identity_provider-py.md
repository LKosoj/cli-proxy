# API Spec: `desktop/services/desktop_identity_provider.py`

Generated: 2026-04-27T22:43:22Z

## Classes
### `class DesktopNotificationTarget` (line 22)

### `class DesktopIdentityProvider` (line 30)
- `def __init__()` (line 33)
- `def owner_id()` (line 45)
- `def list_owned_projects()` (line 48)
- `def require_owned_project(project_slug)` (line 52)
- `def resolve_project_slug(session_uid)` (line 61)
- `def list_project_sessions(project_slug)` (line 68)
- `def list_notification_targets(project_slug)` (line 77)
- `def require_notification_target(project_slug, session_uid)` (line 96)
- `def resolve_session(session_uid)` (line 107)
- `def resolve_mode_launch_actor_chat_id(session)` (line 123)
