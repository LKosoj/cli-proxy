# API Spec: `app/security/auth.py`

Generated: 2026-06-03T02:24:29Z

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

### `class ConfigAuthService` (line 205)
*Thin auth adapter over BotApp access checks plus pluggable auth strategies.*
- `def __init__()` (line 208)
- `def authorize(chat_id)` (line 225)
- `def authenticate(credentials)` (line 248)

## Symbols
- `def build_auth_service(auth_config)` (line 272)
