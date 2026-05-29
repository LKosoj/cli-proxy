# API Spec: `app/events/bus.py`

Generated: 2026-04-27T22:43:22Z

## Classes
### `class BaseSystemEvent` (line 26)
- `def event_name()` (line 30)
- `def to_payload()` (line 33)
- `def from_payload(cls, payload)` (line 39)

### `class GenericSystemEvent(BaseSystemEvent)` (line 49)
- `def event_name()` (line 54)
- `def to_payload()` (line 57)

### `class TelegramIngressEvent(BaseSystemEvent)` (line 63)

### `class WebhookReceivedEvent(BaseSystemEvent)` (line 74)

### `class DesktopCommandEvent(BaseSystemEvent)` (line 88)

### `class MiniAppCommandEvent(BaseSystemEvent)` (line 100)

### `class ScheduledJobEvent(BaseSystemEvent)` (line 113)

### `class ModeLaunchRequestedEvent(BaseSystemEvent)` (line 131)

### `class ModeLaunchCompletedEvent(BaseSystemEvent)` (line 147)

### `class NotificationRequestedEvent(BaseSystemEvent)` (line 162)

### `class ManageTasksChangedEvent(BaseSystemEvent)` (line 176)

### `class SecurityAuditEvent(BaseSystemEvent)` (line 192)

### `class RuntimeConfigReloadedEvent(BaseSystemEvent)` (line 209)

### `class RuntimeConfigReloadFailedEvent(BaseSystemEvent)` (line 221)

### `class _QueuedEvent` (line 232)

### `class SystemEventBus` (line 237)
*Async queue-based system event bus with typed events and handler isolation.*
- `def __init__(logger)` (line 240)
- `def subscribe(event, handler)` (line 249)
- `async def publish(event, payload)` (line 274)
- `async def drain()` (line 285)
- `async def shutdown()` (line 291)
- `def known_event_types()` (line 404)
