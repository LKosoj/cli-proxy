# Desktop Application

CLI Proxy Desktop — клиентское приложение с GUI (PySide6/Qt) для управления CLI-агентами.

## Desktop Mode Launch Policy

Desktop event-driven запуски режимов обрабатываются fail-closed.

### Per-User Allowlist

Desktop launch разрешается только если `DesktopIdentityProvider` смог явно резолвить owning actor в numeric Telegram `chat_id`.

Если actor не резолвится, launch уходит в deny-by-default с reason `actor_unresolved`.

### Реализация

`desktop/services/application_facade.py` вычисляет `launch_policy` до публикации `DesktopCommandEvent`:

```python
actor_chat_id = provider.resolve_mode_launch_actor_chat_id(session)
if actor_chat_id is None:
    reason = "actor_unresolved"
    is_mode_allowed = False
else:
    is_mode_allowed = access_policy_service.is_mode_allowed_for_chat(actor_chat_id, mode_id)
    reason = "" if is_mode_allowed else "mode_not_allowed"
```

`app/services/mode_launch_adapter.py` читает этот `launch_policy` и передаёт `is_mode_allowed` в `SecurityFacade.authorize_mode_launch()`.

Это означает, что Desktop mode launches:
- По умолчанию запрещены, если Desktop session не привязана к явному Telegram actor
- При наличии resolved actor используют тот же `user_modes` allowlist, что и callback-path
- Сохраняют diagnostic deny reasons `actor_unresolved` и `mode_not_allowed`

### Безопасность

Desktop запуски защищены на других уровнях:
1. **Project ownership** — DesktopIdentityProvider проверяет, что пользователь владеет проектом
2. **Session ownership** — DesktopIdentityProvider проверяет, что пользователь владеет сессией
3. **ModeLaunchPolicy** — desktop origin должен быть разрешён в policy allowlist
4. **SecurityFacade** — получает уже вычисленный `is_mode_allowed`

### Сравнение с другими origins

| Origin | is_mode_allowed | Per-user allowlist |
|---------|-----------------|---------------------|
| Telegram callback | Проверяется через `access_policy_service.is_mode_allowed_for_chat` | ✓ |
| MiniApp event | Проверяется через `access_policy_service.is_mode_allowed_for_chat` | ✓ |
| Desktop | `False` без explicit actor resolution; иначе через `access_policy_service.is_mode_allowed_for_chat` | ✓ |
| Scheduler | Всегда `True` (наследует от owner) | ✗ (по дизайну) |
| Webhook | Всегда `True` (наследует от owner) | ✗ (по дизайну) |

### Actor Resolution

Текущий `DesktopIdentityProvider` резолвит actor только из явных session hints (`mode_launch_actor_chat_id`, `owner_chat_id`, `telegram_chat_id`, non-zero `chat_id` или `conversation_scope.chat_id`).

Если таких hints нет, это не silently-allow path: event launch отклоняется.
