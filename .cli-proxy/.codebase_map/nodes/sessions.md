# Node: sessions

Generated: 2026-04-27T22:43:23Z

## Purpose
`sessions/**` is the session-service layer around `session.Session`. It defines Telegram/Desktop conversation scope identifiers, session-scoped storage keys, compatibility accessors for mode/orchestrator/SSH/remote-control state, prompt and mode run orchestration, outbound output delivery, and Telegram session UI/status helpers.

## Scope
- Source glob: `sessions/**`
- Current files: 10 under `sessions/**` as of last review.
- Scope identity and keys: `sessions/conversation_scope.py`, `sessions/scoped_key.py`.
- Runtime state access/reset helpers: `sessions/session_state_access.py`.
- Queue item contract: `sessions/queue_item.py`.
- Bot-facing session facade and runtime services: `sessions/session_management.py`, `sessions/session_run_service.py`, `sessions/session_output_service.py`.
- Telegram session menu/status UI: `sessions/session_ui.py`, `sessions/session_status.py`.
- Primary external model dependency: `session.py` (`Session`, `SessionManager`, `session_runtime_uid`, CLI switching).

## Instructions for agent
- Start with `.cli-proxy/.codebase_map/INDEX.md`, then this node, then the task-specific files under `sessions/**`.
- Before claiming runtime behavior, inspect the exact method/function in source and cite concrete `path:line`.
- For prompt execution changes, inspect `sessions/session_run_service.py` plus `session.py`; verify queue draining, `run_lock`, CLI switching, assistant preview, task tracking, and persistence behavior in the changed path.
- For mode execution changes, inspect `sessions/session_run_service.py`, `modes/DEVELOPMENT.md`, and the affected mode under `modes/**`; keep mode logic on SDK/`BaseMode` contracts rather than adding shared mode behavior to `BotApp`.
- For output delivery changes, inspect `sessions/session_output_service.py`, `app/services/notification_queue_service.py`, `tg/markdown.py`, and `app/services/telegram_transport.py`; preserve Telegram thread routing through `ConversationScope`/`message_thread_id`.
- For session UI/status/state changes, inspect `sessions/session_ui.py`, `sessions/session_status.py`, `sessions/session_state_access.py`, plus callers in `tg/**`, `miniapp/routes.py`, and `desktop/**`.
- Validate Python edits with targeted `.venv/bin/pytest -q` tests near the changed behavior and `.venv/bin/flake8`.

## Source of truth
- `sessions/conversation_scope.py` - `ConversationScope` and `DesktopScope`, canonical `session_uid`/`session_surface`, payload conversion for Telegram chats/topics and Desktop sessions.
- `sessions/scoped_key.py` - scoped-key token sanitization and `session_scoped_key()` derivation used by sandboxes, plans, mode runtime, and Desktop.
- `sessions/session_state_access.py` - active mode, analyst mode, SSH, remote-control, and advanced-orchestrator accessors across nested state and older flat session fields; runtime-state reset helper.
- `sessions/queue_item.py` - typed queue item dataclass and normalizer for legacy string, mapping, and dataclass queue payloads.
- `sessions/session_management.py` - `SessionManagement` facade installed by `bot.py`; composes output, run, interrupt, persistence, HTML rendering, and CLI dialog logging services.
- `sessions/session_run_service.py` - direct prompt execution, mode pipeline execution, queued input dispatch, assistant preview, runtime progress, orchestrator handoff proposal, task scheduling, and persistence.
- `sessions/session_output_service.py` - short-output Telegram sends, long-output HTML document rendering, summary sending, per-scope notification queue integration, and state summary persistence.
- `sessions/session_ui.py` - Telegram session menu callbacks for pick/status/rename/resume/CLI/state/queue/reset/orchestrator/close actions.
- `sessions/session_status.py` - status text and mode-stage builders shared by Telegram, MiniApp, and mode status services.
- `sessions/__init__.py` - package marker only.
- API mirrors: `.cli-proxy/.codebase_map/api/sessions/*.md`.

## When to update
- Any change under `sessions/**`.
- Any change in `session.py` that changes `Session`, `SessionManager`, `session_runtime_uid`, `session_scoped_key`, CLI selection/switching, persistence, queue, lock, or conversation-scope behavior.
- Any change in `bot.py` that changes `SessionManagement`/`SessionUI` construction, Telegram callback scope resolution, `_handle_user_input`, `send_output`, or session cleanup.
- Any change in `app/services/session_service.py`, `app/services/session_run_service.py`, `app/services/input_dispatch_service.py`, `app/services/mode_launch_adapter.py`, `app/services/session_interrupt_service.py`, `app/services/notification_queue_service.py`, `app/services/telegram_transport.py`, `app/services/state_repository.py`, `app/services/session_thread_manager.py`, `app/services/message_buffer_service.py`, or SSH/remote-control services consumed by session state/status helpers.
- Any change in `tg/**`, `miniapp/routes.py`, or `desktop/**` that changes session menus, active mode, session status, SSH/remote-control/orchestrator toggles, output routing, or session selection.
- Any change in `modes/**` that changes mode launch, active mode state, mode status, mode output delivery, scoped keys, queued input, or orchestrator handoff contracts.
- Any targeted test change that adds, removes, or materially changes session coverage under `tests/test_session*.py`, `tests/test_send_output*.py`, `tests/test_orchestrator_post_run_transition.py`, `tests/test_telegram_outbound_thread_routing.py`, `tests/test_notification_queue_service.py`, or Desktop/MiniApp session-state tests.

## Related nodes
- `.cli-proxy/.codebase_map/nodes/session-py.md` - owns `Session`, `SessionManager`, runtime UID/scoped-key integration, CLI process execution, persistence, queue, and conversation scope storage.
- `.cli-proxy/.codebase_map/nodes/bot-py.md` - constructs `SessionManagement`/`SessionUI` and provides BotApp methods called by session services.
- `.cli-proxy/.codebase_map/nodes/app.md` - services used by sessions for orchestration, interrupt, notification queue, state repository, session threads, Telegram transport, SSH, remote control, and runtime progress.
- `.cli-proxy/.codebase_map/nodes/tg.md` - Telegram handlers and callbacks consume session status/state accessors and route session actions.
- `.cli-proxy/.codebase_map/nodes/miniapp.md` - MiniApp routes render session status and use session scoped keys/state for runs, files, logs, SSH, and remote-control UI.
- `.cli-proxy/.codebase_map/nodes/desktop.md` - Desktop facade/widgets use session state accessors, scoped keys, status data, and Desktop session scopes.
- `.cli-proxy/.codebase_map/nodes/modes.md` - mode SDK/runtime and mode plugins consume active mode state, session scoped keys, mode status helpers, and mode pipeline contracts.
- `.cli-proxy/.codebase_map/nodes/utils.md` - path/text helpers used for sandbox session directories, ANSI stripping, and previews.
- `.cli-proxy/.codebase_map/nodes/summary-py.md` - summary function injected into `SessionOutputService` for long-output previews.
- `.cli-proxy/.codebase_map/nodes/tests.md` - targeted coverage for session runtime, output delivery, thread routing, scoped keys, state migration, SSH/remote-control state, and orchestrator handoff.

## Last reviewed
- 2026-05-10
