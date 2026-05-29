# API Spec: `app/services/config_service.py`

Generated: 2026-04-27T22:43:22Z

## Classes
### `class ConfigProvider(ABC)` (line 16)
*Абстракция источника конфигурации для async-сценариев.*
- `async def load()` (line 20)
  - *Загружает полную конфигурацию приложения.*
- `async def get(key, default)` (line 24)
  - *Возвращает значение по dotted-ключу.*

### `class FileConfigProvider(ConfigProvider)` (line 28)
*Провайдер конфигурации из YAML-файла.*
- `def __init__(path)` (line 31)
- `async def load()` (line 35)
- `async def get(key, default)` (line 40)

### `class AppRuntimeParams` (line 59)

### `class ConfigSaveResult` (line 69)

### `class ConfigService` (line 76)
- `def __init__(provider, logger)` (line 77)
- `def config()` (line 83)
  - *Загруженная конфигурация (None до первого load()).*
- `async def load()` (line 87)
- `async def get_value(key, default)` (line 92)
- `async def is_feature_enabled(flag_name)` (line 95)
  - *Централизованная проверка feature flags из defaults.*.*
- `async def serialize_config(config)` (line 114)
- `async def serialize_disk_config(path)` (line 119)
- `async def diff_against_disk(config)` (line 124)
- `def generate_diff(before, after)` (line 131)
- `async def save_atomic(config)` (line 141)
- `async def resolve_runtime_params(config)` (line 182)
- `async def validate_required_secrets(config)` (line 205)
