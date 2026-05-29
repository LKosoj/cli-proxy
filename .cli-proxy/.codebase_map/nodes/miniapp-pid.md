# Node: miniapp.pid

Generated: 2026-04-27T22:43:23Z

## Purpose
Tracks the retired repository-root `miniapp.pid` artifact. It was removed from the git index as PID runtime trash; local PID files are ignored by `.gitignore`.

## Scope
- Source glob: `miniapp.pid`
- Current files: 0 under `miniapp.pid` as of last review.

## Instructions for agent
- Read only files relevant to the active task.
- Prefer deterministic checks before edits.
- Keep changes minimal and validate with tests/linters where applicable.
- Do not re-add `miniapp.pid` to the index; inspect `docs/runtime-artifacts-policy.md` first.

## Source of truth
- `docs/runtime-artifacts-policy.md`
- `.gitignore`

## When to update
- Any commit touching `miniapp.pid`.
- Any architecture or behavior change affecting this area.

## Related nodes
- (none)

## Owner
- project-maintainers

## Last reviewed
- 2026-05-01
