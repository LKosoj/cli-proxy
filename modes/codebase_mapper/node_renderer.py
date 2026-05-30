"""Node and index rendering helpers extracted from CodebaseMapperRuntime.

All functions in this module are free of instance state — they take only
explicit arguments.
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Dict, List, Sequence, Tuple

# ── constants (must stay in sync with runtime.py) ──────────────────────────
_PROMPT_CHANGED_CAP_FAST: int = 8
_PROMPT_INDEX_CAP_FAST: int = 24
_PROMPT_CHANGED_CAP_DEEP: int = 40
_PROMPT_INDEX_CAP_DEEP: int = 80
_PROMPT_CHANGED_CAP_DEFAULT: int = 20
_PROMPT_INDEX_CAP_DEFAULT: int = 40

_DOC_NAMES: Tuple[str, ...] = (
    "STACK.md",
    "INTEGRATIONS.md",
    "ARCHITECTURE.md",
    "STRUCTURE.md",
    "CONVENTIONS.md",
    "TESTING.md",
    "CONCERNS.md",
)


# ── shared utility ──────────────────────────────────────────────────────────

def _utc_now_iso() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# ── C2 rendering candidates ─────────────────────────────────────────────────

def _prompt_caps(*, operation: str) -> Tuple[int, int]:
    op = str(operation or "").strip().lower()
    if op == "run":
        return _PROMPT_CHANGED_CAP_FAST, _PROMPT_INDEX_CAP_FAST
    if op in {"verify", "init_full"}:
        return _PROMPT_CHANGED_CAP_DEEP, _PROMPT_INDEX_CAP_DEEP
    return _PROMPT_CHANGED_CAP_DEFAULT, _PROMPT_INDEX_CAP_DEFAULT


def _review_touched_items(
    *,
    review_items: Sequence[str],
    changed_files: Sequence[str],
    operation: str,
    is_first_init: bool = False,
) -> List[str]:
    if operation in {"init", "init_full"}:
        if is_first_init:
            return []
        return list(review_items)
    changed = [str(p).replace("\\", "/").strip("/") for p in list(changed_files or []) if str(p).strip()]
    if not changed:
        return []
    touched_domains = {p.split("/", 1)[0] for p in changed}
    touched: List[str] = []
    for item in review_items:
        name = str(item).replace("\\", "/").split("/")[-1]
        if not name.endswith(".md"):
            continue
        slug = name[:-3]
        if slug in {"workspace"}:
            if changed:
                touched.append(item)
            continue
        domain = slug.replace("-", "_")
        if slug in touched_domains or domain in touched_domains:
            touched.append(item)
    return touched


def _render_graph_index(
    *,
    node_entries: Sequence[Dict[str, Any]],
    map_dir: str,
    changed_files: Sequence[str],
) -> str:
    doc_descriptions = {
        "STACK.md": "Технологический стек, зависимости, рантаймы и инфраструктурные маркеры.",
        "INTEGRATIONS.md": "Внешние/внутренние интеграции, точки входа и контракты взаимодействий.",
        "ARCHITECTURE.md": "Архитектурная структура модулей, слои и их ответственность.",
        "STRUCTURE.md": "Физическая структура репозитория и индексация значимых путей.",
        "CONVENTIONS.md": "Кодовые конвенции, практики и стандарты реализации.",
        "TESTING.md": "Подход к тестированию, расположение тестов и проверочные правила.",
        "CONCERNS.md": "Риски, технический долг и зоны повышенного внимания.",
    }
    lines = [
        "# Codebase Mapper Instruction Graph",
        "",
        f"Generated: {_utc_now_iso()}",
        "",
        "This index is the entrypoint for agent instructions.",
        "",
        "## Mandatory Workflow",
        "1. Before any edits, read this `INDEX.md` completely.",
        "2. Determine relevant area(s) and open matching files under `.cli-proxy/.codebase_map/nodes/*.md`.",
        "3. Only then inspect source files and implement changes.",
        "4. After changes, update affected node metadata (`When to update`, `Last reviewed`).",
        "5. If node update fails, run targeted repair for that node.",
        "",
        "## Runtime Verification and Fallback Policy (Hardcoded)",
        (
            "- Перед любым утверждением о runtime-поведении ОБЯЗАТЕЛЬНО "
            "проверить конкретный метод/функцию в коде и сослаться на файл:строка."
        ),
        (
            "- Запрещено делать выводы по аналогии между этапами пайплайна без "
            "прямой проверки каждого этапа (decompose/dev/review/final audit)."
        ),
        "- Если вопрос про «кто/когда вызывается», отвечать в формате пошаговой цепочки: шаг -> метод -> исполнитель -> зачем.",
        "- При обнаружении своей неточности сначала коротко исправить факт, затем дать проверенные ссылки на код, без догадок.",
        "- Policy matrix по fallback:",
        (
            "- Legacy-потоки (уже существующее поведение в проде): fallback "
            "разрешён для обратной совместимости, но должен логироваться и быть "
            "явно отражён в отчёте."
        ),
        (
            "- Новый функционал и новые mode-сценарии: fallback запрещён по "
            "умолчанию; при ошибке — явный fail с причиной."
        ),
        (
            "- Opt-in fallback: разрешён только после явного согласования с "
            "пользователем в текущей задаче или если он явно приходит как "
            "требование от пользователя."
        ),
        "",
        "## Runtime Files",
        "- `graph.json`: topology and edges.",
        "- `rules.yaml`: update routing rules.",
        "- `state.json`: statuses/queues (`ok|needs_repair|degraded|invalid`).",
        "- `api/`: optional technical interface mirror.",
        "",
        "## Core Docs",
        "These files are mandatory context and must be considered before major edits.",
    ]
    for name in _DOC_NAMES:
        lines.append(f"- `{name}`: {doc_descriptions.get(name, 'Core project context.')}")  # deterministic catalog
    lines.extend([
        "",
        "## Nodes",
    ])
    for item in node_entries:
        title = str(item.get("title") or "")
        path = str(item.get("path") or "")
        count = int(item.get("file_count") or 0)
        source_glob = str(item.get("source_glob") or "**")
        lines.append(f"- [{title}]({path}) - files: {count}, source_glob: `{source_glob}`")
    lines.extend([
        "",
        "## Runtime Inputs",
        f"- map_dir: `{map_dir}`",
        f"- changed_files: {len(list(changed_files or []))}",
        "",
    ])
    return "\n".join(lines)


def _render_graph_node(
    *,
    domain: str,
    rel_node_path: str,
    source_glob: str,
    file_count: int,
    source_samples: Sequence[str],
    related_node_paths: Sequence[str],
    related_source_globs: Sequence[str],
    related_relation_notes: Sequence[str],
    changed_hits: Sequence[str],
    api_links: Sequence[tuple] = (),
) -> str:
    hits = list(changed_hits or [])
    samples = [str(x) for x in list(source_samples or []) if str(x).strip()]
    related_nodes = [str(x) for x in list(related_node_paths or []) if str(x).strip() and str(x) != rel_node_path]
    related_globs = [str(x) for x in list(related_source_globs or []) if str(x).strip()]
    relation_notes = [str(x) for x in list(related_relation_notes or []) if str(x).strip()]
    lines = [
        f"# Node: {domain}",
        "",
        f"Generated: {_utc_now_iso()}",
        "",
        "## Purpose",
        f"Instruction node for `{domain}` area.",
        "",
        "## Scope",
        f"- Source glob: `{source_glob}`",
        f"- Estimated files: {int(file_count)}",
        "",
        "## Instructions for agent",
        "- Read only files relevant to the active task.",
        "- Prefer deterministic checks before edits.",
        "- Keep changes minimal and validate with tests/linters where applicable.",
        "",
        "## Source of truth",
        f"- `{source_glob}`",
        *[f"- `{p}`" for p in samples[:10]],
        "",
    ]

    if api_links:
        lines.extend(["## Module API", "Детальные интерфейсы модулей этой области:", ""])
        for orig, link in api_links:
            lines.append(f"- [{orig}]({link})")
        lines.append("")

    lines.extend([
        "## When to update",
        f"- Any commit touching `{source_glob}`.",
        *[f"- Any commit touching `{g}` because this node has import/call dependency on it." for g in related_globs[:5]],
        "- Any architecture or behavior change affecting this area.",
        "",
        "## Related nodes",
        *([f"- `{p}`" for p in related_nodes[:8]] if related_nodes else ["- (none)"]),
        *([f"- {note}" for note in relation_notes[:8]] if relation_notes else []),
        "",
        "## Owner",
        "- project-maintainers",
        "",
        "## Last reviewed",
        f"- {_utc_now_iso()}",
        "",
    ])
    if hits:
        lines.extend(["## Recent changed files", *[f"- `{p}`" for p in hits[:30]], ""])
    return "\n".join(lines)


def _build_focus_prompt(
    *,
    root: str,
    map_dir: str,
    focus: str,
    target_docs: Sequence[str],
    full_scan: bool,
    changed_files: Sequence[str],
    file_index: Sequence[str],
    templates: Dict[str, str],
    operation: str,
) -> str:
    changed_cap, index_cap = _prompt_caps(operation=operation)
    docs_list = "\n".join(f"- {d}" for d in target_docs)
    mode_label = "полный скан" if full_scan else "инкрементальный апдейт"
    changed_block = "\n".join(f"- `{p}`" for p in changed_files[:changed_cap])
    if not changed_block:
        changed_block = "- (нет списка изменений)"
    index_block = "\n".join(f"- `{p}`" for p in file_index[:index_cap]) if full_scan else ""

    guidance = str(
        templates.get("guidance")
        or (
            "Ты под-агент Codebase Mapper."
            " Обнови только целевые файлы в `.cli-proxy/.codebase_map/`."
            " Пиши на диск; в ответе только короткий отчет."
        )
    )

    if full_scan:
        scan_policy = str(
            templates.get("scan_policy_full")
            or (
                "Режим: полный скан. Используй `rg --files` и выборочное чтение ключевых файлов по фокусу, "
                "чтобы сформировать полную и актуальную картину."
            )
        )
    else:
        scan_policy = str(
            templates.get("scan_policy_incremental")
            or (
                "Режим: инкрементальный апдейт."
                " Сначала changed files."
                " Доп. чтение только при прямой связи."
                " Полный обход запрещен."
            )
        )

    write_rules_tpl = str(
        templates.get("write_rules")
        or (
            "Правила записи:\n"
            "1. Обнови только документы:\n{docs_list}\n"
            "2. Путь для записи: `{map_dir}`\n"
            "3. Формат: короткий markdown с конкретными file-path.\n"
            "4. Если данных мало, явно укажи ограничения."
        )
    )
    write_rules = write_rules_tpl.format(docs_list=docs_list, map_dir=map_dir)

    sections = [
        guidance,
        f"Фокус: `{focus}`",
        f"Проект: `{root}`",
        scan_policy,
        f"Контекст запуска: {mode_label}",
        "Измененные файлы (git diff):",
        changed_block,
        write_rules,
    ]

    if full_scan:
        sections.extend([
            "Индекс файлов (результат rg --files):",
            index_block or "- (пусто)",
        ])

    sections.append(
        str(
            templates.get("report_format")
            or "Верни итог: фокус, обновленные файлы, что дополнительно проверено."
        )
    )
    return "\n\n".join(sections).strip()
