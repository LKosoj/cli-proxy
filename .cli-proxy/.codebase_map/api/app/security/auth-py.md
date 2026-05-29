# API Spec: `app/security/auth.py`

Generated: 2026-04-27T22:43:22Z

## Classes
### `class TokenAuthStrategy` (line 10)
- `def __init__()` (line 13)
- `def authenticate(credentials)` (line 17)

### `class OAuthStrategy` (line 52)
- `def __init__()` (line 55)
- `def authenticate(credentials)` (line 66)

### `class TelegramMiniAppInitDataStrategy` (line 136)
- `def __init__()` (line 139)
- `def authenticate(credentials)` (line 142)

### `class ConfigAuthService` (line 202)
*Thin auth adapter over BotApp access checks plus pluggable auth strategies.*
- `def __init__()` (line 205)
- `def authorize(chat_id)` (line 222)
- `def authenticate(credentials)` (line 245)

## Symbols
- `def build_auth_service(auth_config)` (line 269)
