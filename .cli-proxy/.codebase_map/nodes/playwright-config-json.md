# Node: playwright_config.json

Generated: 2026-04-27T22:43:23Z

## Purpose
Guide agents that maintain the repository-level Playwright browser launch config used for Chromium sandbox/dev-shm flags.

## Scope
- Source glob: `playwright_config.json`
- File: `playwright_config.json`
- Current JSON shape: top-level `launchOptions.args`.
- Current Chromium flags: `--no-sandbox`, `--disable-setuid-sandbox`, `--disable-dev-shm-usage`.

## Instructions for agent
- Read `playwright_config.json` before changing any browser launch flags.
- Keep the file valid JSON; do not add comments or trailing commas.
- Keep edits limited to Playwright launch configuration unless the task explicitly changes browser-test workflow.
- If browser/MiniApp behavior is affected, inspect `tests/test_miniapp_playwright.py`; it creates its own temp `playwright-cli` root config in `_playwright_cli_args()`.
- For web/MiniApp verification changes, follow `nodes/tests.md` and the repository rule requiring `playwright-cli`.

## Source of truth
- `playwright_config.json`
- `tests/test_miniapp_playwright.py`

## When to update
- Any commit touching `playwright_config.json`.
- Any change to expected Chromium/Playwright launch flags for repository-level browser automation.
- Any change that wires this root config into, or removes it from, an in-repository browser test workflow.

## Related nodes
- `nodes/tests.md`

## Last reviewed
- 2026-04-28
