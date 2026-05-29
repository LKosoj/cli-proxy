# Node: full_ui.yaml

Generated: 2026-04-27T22:43:23Z

## Purpose
`/srv/git_projects/cli-proxy/full_ui.yaml` is a retired repository-root YAML snapshot artifact. It was removed from the git index as runtime/UI snapshot trash; local copies are ignored by `.gitignore`. No verified runtime readers exist in the repository.

## Scope
- Source glob: `full_ui.yaml`
- Current files: 0 under `full_ui.yaml` as of last review.
- File: `/srv/git_projects/cli-proxy/full_ui.yaml` only when present as a local ignored artifact.
- Includes only this root-level YAML file.
- Excludes runtime configuration files such as `/srv/git_projects/cli-proxy/config.yaml` and `/srv/git_projects/cli-proxy/config_example.yaml`.
- Excludes MiniApp, Desktop, bot, and mode UI code unless a concrete reference to `full_ui.yaml` is added.

## Instructions for agent
- Start with `/srv/git_projects/cli-proxy/.cli-proxy/.codebase_map/INDEX.md`, then this node, then `docs/runtime-artifacts-policy.md`.
- Do not infer runtime behavior from this file name; first verify concrete readers with `rg -n "full_ui\\.yaml|full_ui" /srv/git_projects/cli-proxy`.
- Keep edits limited to `/srv/git_projects/cli-proxy/full_ui.yaml` unless the active task explicitly adds or updates a reader.
- If the YAML content changes, verify it is still parseable before reporting success.

## Source of truth
- `docs/runtime-artifacts-policy.md` - explains why root `full_ui.yaml` is not tracked.
- `.gitignore` - ignores root `full_ui.yaml` after index cleanup.
- `/srv/git_projects/cli-proxy/.cli-proxy/.codebase_map/INDEX.md` - graph workflow and node update rules.

## When to update
- Any change to `/srv/git_projects/cli-proxy/full_ui.yaml`.
- Any new code path, script, test, documentation, or UI flow that starts reading, generating, validating, or documenting `/srv/git_projects/cli-proxy/full_ui.yaml`.
- Any codebase-map routing change that changes ownership or source glob coverage for `full_ui.yaml`.

## Related nodes
- None verified in `/srv/git_projects/cli-proxy/.cli-proxy/.codebase_map/graph.json`.

## Last reviewed
- 2026-05-01
