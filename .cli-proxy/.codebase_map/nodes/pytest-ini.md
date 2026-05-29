# Node: pytest.ini

Generated: 2026-04-27T22:43:23Z

## Purpose
Document the repository-wide pytest configuration in root `pytest.ini`: async fixture loop scope and registered markers used by tests.

## Scope
- Source glob: `pytest.ini`
- Current files: 1 root-level config file.
- Covers `[pytest]` settings only: `asyncio_default_fixture_loop_scope = function` and registered `asyncio`/`ssh_integration` markers.
- Does not cover fixtures in `conftest.py` or `tests/conftest.py`; use `nodes/conftest-py.md` and `nodes/tests.md` for those areas.

## Instructions for agent
- Read `pytest.ini` before changing pytest behavior, async test behavior, or pytest marker registration.
- Keep pytest configuration minimal and repository-wide; do not add test-specific behavior here when it belongs in `conftest.py`, `tests/conftest.py`, or a focused test file.
- When changing async pytest settings or markers, verify with targeted `.venv/bin/pytest -q ...` tests that exercise the affected async or marked tests.
- Follow `.cli-proxy/.codebase_map/TESTING.md` for pytest command selection and full-suite limits.

## Source of truth
- `pytest.ini`
- `.cli-proxy/.codebase_map/TESTING.md`
- `tests/**`

## When to update
- Any commit touching `pytest.ini`.
- Any change to repository pytest defaults, async fixture loop scope, or registered pytest markers.
- Any change to `.cli-proxy/.codebase_map/TESTING.md` that changes how agents should interpret or verify `pytest.ini`.

## Related nodes
- `nodes/tests.md`
- `nodes/conftest-py.md`

## Last reviewed
- 2026-04-28
