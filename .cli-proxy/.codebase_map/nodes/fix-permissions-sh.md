# Node: fix-permissions.sh

Generated: 2026-04-27T22:43:23Z

## Purpose
Instruction node for the root-level maintenance script `fix-permissions.sh`.
The script repairs group ownership and group permissions for `/srv/git_projects`
or a caller-provided target directory, using group `cli-proxy-workgroup`.

## Scope
- Source file: `fix-permissions.sh`
- Default target directory: `/srv/git_projects`
- Covered behavior: target directory existence check, recursive `chgrp`,
  recursive `chmod g+rw`, directory `chmod g+xs`, and post-run counts for
  files/directories still missing group write permission.
- Out of scope: setup automation in `scripts/setup-claude-bot.sh`, README
  installation docs, and Python runtime configuration.

## Instructions for agent
- Read `fix-permissions.sh` before changing this node or the script.
- Treat this script as a filesystem-permission tool. Do not run it during
  documentation updates unless the user explicitly asks.
- Keep edits surgical and preserve the positional argument behavior
  `TARGET_DIR="${1:-/srv/git_projects}"` unless the task explicitly changes it.
- For script changes, verify shell syntax with `bash -n fix-permissions.sh`.
- If changing permission semantics, state the affected `chgrp`, `chmod`, or
  `find` command explicitly.

## Source of truth
- `fix-permissions.sh`
- `.cli-proxy/.codebase_map/INDEX.md`
- `.cli-proxy/.codebase_map/rules.yaml`

## When to update
- Any commit touching `fix-permissions.sh`.
- Any change to the default target `/srv/git_projects`, group name
  `cli-proxy-workgroup`, or permission commands in `fix-permissions.sh`.
- Any change to this node's map routing in `.cli-proxy/.codebase_map/INDEX.md`
  or `.cli-proxy/.codebase_map/rules.yaml`.

## Related nodes
- None declared in `.cli-proxy/.codebase_map/graph.json`; this node is only
  referenced from the map index.

## Owner
- project-maintainers

## Last reviewed
- 2026-04-28
