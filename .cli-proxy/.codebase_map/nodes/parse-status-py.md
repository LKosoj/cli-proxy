# Node: parse_status.py

Generated: 2026-04-27T22:43:23Z

## Purpose
Root-level helper script that reads `miniapp/routes.py`, searches the file text with a regular expression for a status-payload return block containing `"resume_tokens"`, and prints either the matched fragment or `Could not find the payload dict`.

## Scope
- Source glob: `parse_status.py`
- Estimated files: 1
- Runtime input: `miniapp/routes.py`
- Dependencies: Python stdlib `re`

## Instructions for agent
- Start with `.cli-proxy/.codebase_map/INDEX.md`, then this node, then `parse_status.py`.
- Before describing behavior, inspect the exact regex and file path used in `parse_status.py`; do not infer MiniApp runtime behavior from this helper.
- If changing what the helper extracts, verify the current MiniApp status payload shape in `miniapp/routes.py`.
- Keep changes surgical; this script has no dedicated tests in the current tree.

## Source of truth
- `parse_status.py` - script implementation, regex, printed output, and hardcoded input path.
- `miniapp/routes.py` - source text scanned by the helper.

## When to update
- Any commit touching `parse_status.py`.
- Any change in `miniapp/routes.py` that renames, moves, or reshapes the status payload block this script is intended to inspect.
- Any change to the helper's invocation, output contract, or input file path.

## Related nodes
- `nodes/miniapp.md` - owns `miniapp/routes.py`, the file scanned by `parse_status.py`.

## Last reviewed
- 2026-04-27T23:27:43Z
