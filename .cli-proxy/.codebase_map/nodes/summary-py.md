# Node: summary.py

Generated: 2026-04-27T22:43:23Z

## Purpose
`/srv/git_projects/cli-proxy/summary.py` centralizes OpenAI-backed text summaries and git commit message suggestions. It prepares long CLI output for Telegram/session previews, strips CLI preambles, normalizes text, reuses cached AsyncOpenAI clients, and parses structured commit-message JSON.

## Scope
- Source glob: `summary.py`
- File: `/srv/git_projects/cli-proxy/summary.py`
- Includes: `summarize_text()`, `summarize_text_with_reason()`, `suggest_commit_message_async()`, `suggest_commit_message_detailed_async()`, sync commit-message wrappers, OpenAI config lookup/cache helpers, tail digest extraction, and commit-message JSON validation.
- Excludes: context-window compression in `/srv/git_projects/cli-proxy/modes/sdk/runtime/context_summarizer.py`, Telegram output orchestration in `/srv/git_projects/cli-proxy/sessions/session_output_service.py`, and git UI flows in `/srv/git_projects/cli-proxy/app/services/git_ops_service.py` or `/srv/git_projects/cli-proxy/desktop/widgets/git_panel.py`.

## Instructions for agent
- Read `/srv/git_projects/cli-proxy/summary.py` before changing this area.
- For summary behavior, also inspect `/srv/git_projects/cli-proxy/config.py`, `/srv/git_projects/cli-proxy/utils/text.py`, and the caller path that sends the summary (`/srv/git_projects/cli-proxy/bot.py` or `/srv/git_projects/cli-proxy/sessions/session_output_service.py`).
- For commit-message behavior, also inspect `/srv/git_projects/cli-proxy/modes/sdk/runtime/json_normalizer.py`, `/srv/git_projects/cli-proxy/app/services/git_ops_service.py`, and `/srv/git_projects/cli-proxy/desktop/widgets/git_panel.py`.
- Keep OpenAI runtime behavior on `config.defaults.openai_*` with environment fallback only as implemented in `/srv/git_projects/cli-proxy/summary.py`; do not introduce separate OpenAI config paths.
- Validate Python edits with targeted tests such as `/srv/git_projects/cli-proxy/tests/test_summary_uses_big_model.py`, `/srv/git_projects/cli-proxy/tests/test_summary_commit_message_language.py`, and the nearest changed caller tests.

## Source of truth
- `/srv/git_projects/cli-proxy/summary.py` - runtime behavior for summary generation, OpenAI client reuse, OpenAI config fallback, tail digest, and commit-message generation.
- `/srv/git_projects/cli-proxy/.cli-proxy/.codebase_map/api/summary-py.md` - generated public symbol inventory only; verify behavior in source.
- `/srv/git_projects/cli-proxy/config.py` - `AppConfig`/`defaults.openai_api_key`, `defaults.openai_big_model`, and `defaults.openai_base_url` consumed by `summary.py`.
- `/srv/git_projects/cli-proxy/modes/sdk/runtime/openai_client.py` - AsyncOpenAI client factory used by `summary.py`.
- `/srv/git_projects/cli-proxy/modes/sdk/runtime/json_normalizer.py` - structured JSON parsing/validation used for detailed commit messages.
- `/srv/git_projects/cli-proxy/utils/text.py` - text normalization used before summarization.

## When to update
- Any change to `/srv/git_projects/cli-proxy/summary.py`.
- Any change to OpenAI defaults or fallback semantics in `/srv/git_projects/cli-proxy/config.py`, `/srv/git_projects/cli-proxy/config.yaml`, or `/srv/git_projects/cli-proxy/config_example.yaml`.
- Any change to `/srv/git_projects/cli-proxy/modes/sdk/runtime/openai_client.py`, `/srv/git_projects/cli-proxy/modes/sdk/runtime/json_normalizer.py`, or `/srv/git_projects/cli-proxy/utils/text.py` that changes behavior consumed by `summary.py`.
- Any change in `/srv/git_projects/cli-proxy/bot.py`, `/srv/git_projects/cli-proxy/sessions/session_management.py`, `/srv/git_projects/cli-proxy/sessions/session_output_service.py`, `/srv/git_projects/cli-proxy/app/services/git_ops_service.py`, `/srv/git_projects/cli-proxy/desktop/widgets/git_panel.py`, or `/srv/git_projects/cli-proxy/modes/sdk/runtime/context_summarizer.py` that changes how these functions are called.
- Any targeted test change that adds, removes, or materially changes coverage for summary generation, OpenAI config selection, OpenAI retries, or commit-message JSON parsing.

## Related nodes
- `/srv/git_projects/cli-proxy/.cli-proxy/.codebase_map/nodes/config-py.md`
- `/srv/git_projects/cli-proxy/.cli-proxy/.codebase_map/nodes/config-example-yaml.md`
- `/srv/git_projects/cli-proxy/.cli-proxy/.codebase_map/nodes/bot-py.md`
- `/srv/git_projects/cli-proxy/.cli-proxy/.codebase_map/nodes/sessions.md`
- `/srv/git_projects/cli-proxy/.cli-proxy/.codebase_map/nodes/app.md`
- `/srv/git_projects/cli-proxy/.cli-proxy/.codebase_map/nodes/desktop.md`
- `/srv/git_projects/cli-proxy/.cli-proxy/.codebase_map/nodes/modes.md`
- `/srv/git_projects/cli-proxy/.cli-proxy/.codebase_map/nodes/utils.md`
- `/srv/git_projects/cli-proxy/.cli-proxy/.codebase_map/nodes/tests.md`

## Last reviewed
- 2026-05-04
