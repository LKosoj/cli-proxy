# Node: skills-lock.json

Generated: 2026-04-27T22:43:23Z

## Purpose
Describes the repository-level skill lock file that records pinned skill metadata for this project. The current lock contains the `shadcn` skill entry with its source, source type, and computed hash.

## Scope
- Source glob: `skills-lock.json`
- Estimated files: 1
- In scope: root file `skills-lock.json`.
- Out of scope: installed skill manifests under `.cli-proxy/skills/**` and skill runtime code under `app/services/**`.

## Instructions for agent
- Before changing the lock, inspect `skills-lock.json` directly and preserve valid JSON formatting.
- Keep entries deterministic: skill id, `source`, `sourceType`, and `computedHash` should reflect the exact locked source.
- Do not infer runtime behavior from this file alone; verify any runtime claim in the relevant code before documenting it.
- If the lock changes because a skill was added, removed, or re-pinned, update this node's `Last reviewed` date in the same change.

## Source of truth
- `skills-lock.json`
- `.cli-proxy/.codebase_map/rules.yaml` entry `update-nodeskills-lock-json`
- `.cli-proxy/.codebase_map/graph.json` node `node:skills-lock-json`

## When to update
- Any commit touching root `skills-lock.json`.
- When a locked skill id, source, source type, or computed hash changes.
- When Codebase Mapper routing for `skills-lock.json` changes in `.cli-proxy/.codebase_map/rules.yaml` or `.cli-proxy/.codebase_map/graph.json`.

## Related nodes
- `.cli-proxy/.codebase_map/nodes/app.md` - skill runtime and registry services live under `app/services/**`.
- `.cli-proxy/.codebase_map/nodes/config-py.md` - default skill registry and allowlist settings are defined in `config.py`.
- `.cli-proxy/.codebase_map/nodes/config-example-yaml.md` - example values for skill discovery and registry settings live in `config_example.yaml`.

## Last reviewed
- 2026-04-28
