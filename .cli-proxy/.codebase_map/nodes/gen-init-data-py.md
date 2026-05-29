# Node: gen_init_data.py

Generated: 2026-04-27T22:43:23Z

## Purpose
Guide agents working with the root helper `gen_init_data.py`, which builds a signed Telegram MiniApp `initData` query string for a supplied bot token and user id.

## Scope
- Source glob: `gen_init_data.py`
- File: `gen_init_data.py`
- Public helper: `build_init_data(bot_token: str, user_id: int) -> str`
- CLI behavior: running `python gen_init_data.py` prints one sample signed `initData` string.
- Map API mirror: `.cli-proxy/.codebase_map/api/gen_init_data-py.md`

## Instructions for agent
- Read `gen_init_data.py` before claiming behavior or changing the generated fields/signature.
- Keep the helper compatible with `miniapp/auth.py::verify_telegram_init_data` when changing HMAC derivation, payload fields, URL encoding, or `user` JSON shape.
- Treat `gen_init_data.py` as a standalone local helper; do not infer MiniApp runtime authentication behavior from it without checking `miniapp/auth.py`.
- For behavior edits, run a direct CLI check with `python gen_init_data.py`; run targeted MiniApp auth/routes tests only if runtime verification compatibility changes.

## Source of truth
- `gen_init_data.py` - helper implementation and sample `__main__` output.
- `.cli-proxy/.codebase_map/api/gen_init_data-py.md` - mapper API symbol mirror for `build_init_data`.
- `miniapp/auth.py` - runtime Telegram MiniApp `initData` verification semantics that generated data should satisfy.

## When to update
- Any change to `gen_init_data.py`.
- Any change to Telegram MiniApp `initData` signing, encoding, required fields, or user payload handling in `miniapp/auth.py`.
- Any change that adds, removes, or renames direct tests or documented workflows for generated MiniApp `initData`.

## Related nodes
- `.cli-proxy/.codebase_map/nodes/miniapp.md` - covers `miniapp/auth.py` and MiniApp routes that consume Telegram `initData`.
- `.cli-proxy/.codebase_map/nodes/tests.md` - covers MiniApp auth/route tests with local signed `initData` helpers.

## Last reviewed
- 2026-04-27T23:23:05Z
