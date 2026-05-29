# Node: SESSION.json

Generated: 2026-04-27T22:43:23Z

## Purpose
Tracks the retired repository-root `SESSION.json` artifact. It was removed from the git index as runtime-like trash; local copies are ignored by `.gitignore`. Do not infer runtime session persistence from this filename alone.

## Scope
- Source glob: `SESSION.json`
- Current files: 0 under `SESSION.json` as of last review.
- File: `SESSION.json` at repository root only when present as a local ignored artifact.
- Excludes per-workdir/per-sandbox `SESSION.json` files read or written by `modes/**`, including `modes/sdk/orchestrator_runner.py`, `modes/sdk/runtime/agent_core.py`, `modes/sdk/runtime/memory_retrieval.py`, and `modes/analyst/draft_service.py`.
- Excludes bot session inventory persistence, which uses `config.defaults.state_path` through `session.py` and defaults to `state.json` in `config.py`, `config.yaml`, and `config_example.yaml`.

## Instructions for agent
- Start with `.cli-proxy/.codebase_map/INDEX.md`, then this node, then inspect `docs/runtime-artifacts-policy.md`.
- Before editing, verify whether `SESSION.json` is still tracked and whether it is still empty with `git ls-files --stage SESSION.json` and `ls -l SESSION.json`.
- Do not populate, delete, or reformat root `SESSION.json` as cleanup unless the task explicitly asks for this artifact.
- If the task is about runtime session restore or persistence, inspect `session.py`, `app/services/state_repository.py`, and `config.defaults.state_path` instead of assuming root `SESSION.json` is the runtime store.
- If the task is about Analyst/Orchestrator memory, evidence, or drafts, inspect the `modes/**` paths that join runtime `cwd` or `state_root` with `"SESSION.json"`.

## Source of truth
- `docs/runtime-artifacts-policy.md` - explains why root `SESSION.json` is not tracked.
- `.gitignore` - ignores root `/SESSION.json` after index cleanup.
- `.cli-proxy/.codebase_map/rules.yaml` - routes root `SESSION.json` changes to this node.
- `session.py` - `SessionManager` uses `config.defaults.state_path`, not root `SESSION.json`, for persisted session inventory.
- `config.py`, `config.yaml`, `config_example.yaml` - define/default `defaults.state_path` (`state.json`).
- `modes/sdk/orchestrator_runner.py` - reads/appends per-runtime `cwd/SESSION.json` entries under `orchestrator_by_task`.
- `modes/sdk/runtime/agent_core.py` - reads/writes per-runtime `state_root/SESSION.json` with `history_by_task`.
- `modes/sdk/runtime/memory_retrieval.py`, `modes/analyst/mode.py`, `modes/analyst/draft_service.py` - read per-runtime `SESSION.json` for memory, Analyst evidence, and draft generation.

## When to update
- Any commit touching repository-root `SESSION.json`.
- Any change to `.cli-proxy/.codebase_map/rules.yaml` routing for `SESSION.json`.
- Any change that intentionally gives root `SESSION.json` content, schema, or runtime meaning.
- Any change in `modes/**` that changes the schema or semantics of per-runtime `SESSION.json` files and affects how agents should distinguish them from root `SESSION.json`.

## Related nodes
- `.cli-proxy/.codebase_map/nodes/modes.md` - owns code that reads/writes per-runtime `SESSION.json` files.
- `.cli-proxy/.codebase_map/nodes/session-py.md` - owns `SessionManager` restore/persist behavior through `defaults.state_path`.
- `.cli-proxy/.codebase_map/nodes/config-py.md` - owns `DefaultsConfig.state_path`.
- `.cli-proxy/.codebase_map/nodes/config-example-yaml.md` - documents example `defaults.state_path`.
- `.cli-proxy/.codebase_map/nodes/tests.md` - includes tests that create temp `SESSION.json` files for mode/runtime behavior.

## Last reviewed
- 2026-05-01
