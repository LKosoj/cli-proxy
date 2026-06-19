# API Spec: `app/security/auth.py`

Generated: 2026-06-17T10:46:18Z

## Classes
### `class TokenAuthStrategy` (line 13)
- `def __init__()` (line 16)
- `def authenticate(credentials)` (line 20)

### `class OAuthStrategy` (line 55)
- `def __init__()` (line 58)
- `def authenticate(credentials)` (line 69)

### `class TelegramMiniAppInitDataStrategy` (line 139)
- `def __init__()` (line 142)
- `def authenticate(credentials)` (line 145)

### `class ConfigAuthService` (line 207)
*Thin auth adapter over BotApp access checks plus pluggable auth strategies.*
- `def __init__()` (line 210)
- `def authorize(chat_id)` (line 227)
- `def authenticate(credentials)` (line 250)

## Symbols
- `def build_auth_service(auth_config)` (line 274)
