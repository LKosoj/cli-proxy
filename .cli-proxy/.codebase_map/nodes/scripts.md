# Node: scripts

Generated: 2026-04-27T22:43:23Z

## Purpose
`/srv/git_projects/cli-proxy/scripts/` contains repository maintenance/setup
shell automation that lives outside the Python bot, Desktop, and MiniApp
runtimes. The current script provisions a dedicated Unix user for Claude Code
CLI-agent work and prepares shared filesystem access under a workdir.

## Scope
- Source glob: `scripts/**`
- Current file: `/srv/git_projects/cli-proxy/scripts/setup-claude-bot.sh`
- Covers `scripts/setup-claude-bot.sh` options `--workdir`, `--username`, and
  `--version`; root-user enforcement; `cli-proxy-workgroup` group setup;
  recursive workdir ownership/permission setup; `curl` dependency check;
  `claude` installation/check for the target user; `.bashrc` PATH and
  Anthropic environment entries; and final validation output.
- Out of scope: `/srv/git_projects/cli-proxy/setup_bot.sh`,
  `/srv/git_projects/cli-proxy/fix-permissions.sh`, Python runtime code, and
  service deployment outside this repository checkout.

## Instructions for agent
- Read `/srv/git_projects/cli-proxy/scripts/setup-claude-bot.sh` before
  changing this node or the script.
- Treat the script as root-level host setup automation. Do not run it during
  documentation or code review tasks unless the user explicitly asks.
- Preserve the documented default values unless the task explicitly changes
  them: `WORKDIR="/srv/git_projects"`, `USERNAME="claude-bot"`,
  `CLAUDE_VERSION="latest"`, and `SHARED_GROUP="cli-proxy-workgroup"`.
- For script changes, verify shell syntax with
  `bash -n scripts/setup-claude-bot.sh`.
- If changing setup semantics, state the affected user, group, workdir,
  permission, install, PATH, or environment-variable behavior explicitly.

## Source of truth
- `/srv/git_projects/cli-proxy/scripts/setup-claude-bot.sh` - executable
  behavior and defaults.
- `/srv/git_projects/cli-proxy/README.md` - Russian user-facing Claude Code
  setup instructions.
- `/srv/git_projects/cli-proxy/README_EN.MD` - English user-facing Claude Code
  setup instructions.
- `/srv/git_projects/cli-proxy/.cli-proxy/.codebase_map/INDEX.md` - map entry
  for the `scripts` node.
- `/srv/git_projects/cli-proxy/.cli-proxy/.codebase_map/rules.yaml` - update
  routing rule for `scripts/**`.

## When to update
- Any commit touching `/srv/git_projects/cli-proxy/scripts/**`.
- Any change to the command-line options, defaults, root requirement, target
  user, shared group, workdir permissions, `claude` install/check flow, PATH
  setup, Anthropic environment entries, or final validation behavior in
  `scripts/setup-claude-bot.sh`.
- Any README change that adds, removes, or changes the documented
  `sudo ./scripts/setup-claude-bot.sh` workflow.
- Any change to `.cli-proxy/.codebase_map/INDEX.md`, `graph.json`, or
  `rules.yaml` that changes this node's source glob, routing, or declared
  relationships.

## Related nodes
- `/srv/git_projects/cli-proxy/.cli-proxy/.codebase_map/nodes/modes.md` -
  declared as related to `node:scripts` in
  `/srv/git_projects/cli-proxy/.cli-proxy/.codebase_map/graph.json`.
- `/srv/git_projects/cli-proxy/.cli-proxy/.codebase_map/nodes/setup-bot-sh.md`
  - separate root-level bot setup script; keep behavior/docs distinct from
  `scripts/setup-claude-bot.sh`.
- `/srv/git_projects/cli-proxy/.cli-proxy/.codebase_map/nodes/fix-permissions-sh.md`
  - separate root-level permission repair script; overlaps only in filesystem
  permission domain.

## Last reviewed
- 2026-04-28
