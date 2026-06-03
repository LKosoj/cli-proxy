# API Spec: `app/services/config_service.py`

Generated: 2026-06-03T02:24:29Z

## Classes
### `class ConfigProvider(ABC)` (line 24)
*Абстракция источника конфигурации для async-сценариев.*
- `async def load()` (line 28)
  - *Загружает полную конфигурацию приложения.*
- `async def get(key, default)` (line 32)
  - *Возвращает значение по dotted-ключу.*

### `class FileConfigProvider(ConfigProvider)` (line 36)
*Провайдер конфигурации из YAML-файла.*
- `def __init__(path)` (line 39)
- `async def load()` (line 43)
- `async def get(key, default)` (line 48)

### `class RuntimeConfigValidator` (line 66)
*Validates runtime config draft payloads against the canonical model.*
- `def validate_draft(draft)` (line 69)

### `class AppRuntimeParams` (line 74)

### `class ConfigSaveResult` (line 84)

### `class ConfigDraftSaveResult` (line 92)

### `class ConfigService` (line 146)
- `def __init__(provider, logger, validator)` (line 147)
- `def config()` (line 159)
  - *Загруженная конфигурация (None до первого load()).*
- `async def load()` (line 163)
- `async def get_value(key, default)` (line 168)
- `async def current_revision(config)` (line 171)
- `async def is_feature_enabled(flag_name)` (line 175)
  - *Централизованная проверка feature flags из defaults.*.*
- `async def serialize_config(config)` (line 194)
- `async def serialize_disk_config(path)` (line 199)
- `async def diff_against_disk(config)` (line 204)
- `def generate_diff(before, after)` (line 211)
- `async def save_atomic(config)` (line 251)
- `async def save_draft_with_revision(draft)` (line 276)
- `async def save_config_draft_with_revision(config)` (line 343)
- `async def resolve_runtime_params(config)` (line 374)
- `async def validate_required_secrets(config)` (line 397)
- `async def set_user_language(user_id, lang)` (line 404)
  - *Set per-user language preference. Idempotent. Retries on revision mismatch.*
- `async def set_default_language(lang)` (line 442)
  - *Set global default language. Used by Desktop. Idempotent.*
