# Node: mkdocs.yml

Generated: 2026-04-27T22:43:23Z

## Purpose
`/srv/git_projects/cli-proxy/mkdocs.yml` configures the MkDocs build for the generated CLI Proxy documentation site: site name, docs source directory, build output directory, plugins, Markdown extensions, Mermaid fence support, and navigation.

## Scope
- Source glob: `mkdocs.yml`
- File: `/srv/git_projects/cli-proxy/mkdocs.yml`
- Covers MkDocs keys in that file: `site_name`, `docs_dir`, `site_dir`, `site_url`, `use_directory_urls`, `plugins`, `markdown_extensions`, and `nav`.
- Configured docs source: `/srv/git_projects/cli-proxy/.ai-docs`; configured build output: `/srv/git_projects/cli-proxy/ai_docs_site`.
- Excludes generated documentation content, runtime code, README files, and Codebase Mapper internals unless `/srv/git_projects/cli-proxy/mkdocs.yml` references change.

## Instructions for agent
- Start with `/srv/git_projects/cli-proxy/.cli-proxy/.codebase_map/INDEX.md`, then this node, then `/srv/git_projects/cli-proxy/mkdocs.yml`.
- Before editing `nav`, `docs_dir`, or `site_dir`, verify affected paths in `/srv/git_projects/cli-proxy/mkdocs.yml` and the corresponding docs files under `/srv/git_projects/cli-proxy/.ai-docs/**` when that generated tree exists.
- Preserve YAML structure and MkDocs plugin/extension names unless the active task changes documentation rendering.
- If changing Mermaid support, keep `plugins.mermaid2.javascript` and `pymdownx.superfences.custom_fences` aligned.
- Validate YAML syntax after edits. When `/srv/git_projects/cli-proxy/.ai-docs` exists and MkDocs is installed in `.venv`, run `.venv/bin/python -m mkdocs build --config-file /srv/git_projects/cli-proxy/mkdocs.yml --strict` or report why it was skipped.
- Do not update `/srv/git_projects/cli-proxy/ai_docs_site` build artifacts unless the active task explicitly asks for generated site output.

## Source of truth
- `/srv/git_projects/cli-proxy/mkdocs.yml` - MkDocs configuration covered by this node.
- `/srv/git_projects/cli-proxy/.ai-docs/**` - generated docs tree named by `docs_dir` when present.
- `/srv/git_projects/cli-proxy/.cli-proxy/.codebase_map/rules.yaml` - update rule `update-nodemkdocs-yml` routes changes to this node.

## When to update
- Any change to `/srv/git_projects/cli-proxy/mkdocs.yml`.
- Any change that renames or relocates the configured docs source `/srv/git_projects/cli-proxy/.ai-docs` or build output `/srv/git_projects/cli-proxy/ai_docs_site`.
- Any generated-docs layout change that requires updating `nav` paths in `/srv/git_projects/cli-proxy/mkdocs.yml`.
- Any change to MkDocs plugin, Markdown extension, Mermaid rendering, `site_url`, or `use_directory_urls` behavior in `/srv/git_projects/cli-proxy/mkdocs.yml`.

## Related nodes
- None verified in `/srv/git_projects/cli-proxy/.cli-proxy/.codebase_map/graph.json`; this node is referenced from `/srv/git_projects/cli-proxy/.cli-proxy/.codebase_map/INDEX.md`.

## Last reviewed
- 2026-04-27T23:25:55Z
