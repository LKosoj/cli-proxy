# lint_evolution

Самоэволюционирующая система линт-правил по фрактальной модели `classify → decide → feedback`.

## Три уровня

| Уровень | Что делает | Кто классифицирует | Когда даст результат |
|---|---|---|---|
| L1 (curator) | сигналы из manager-ревью → правила | активный CLI сессии | с первого прогона |
| L2 (meta-curator) | `notes` → новые поля classification-схемы | активный CLI | ~1–2 недели накопления |
| L3 (regressor) | outcomes → веса decision engine | детерминированный Python | ~6–8 недель накопления |

Решения принимает Python (`lint_decision.decide`, `schema_decision.decide_schema`, `weights_regressor.run_level3`) — CLI только классифицирует.

## Артефакты (runtime)

```
.cli-proxy/lint_evolution/
  state.json              # last_run_ts, lock_owner, consecutive_failures per project_id
  evolution.db            # SQLite: signals / fingerprints / runs / outcomes
  rules/
    self.yaml             # active правила
    decision_weights.yaml # текущие веса L1/L2
    weights_history.yaml  # история обновлений L3
  candidates/
    pending.yaml          # ждут классификации/доработки
    rejected.yaml         # отклонены, источник notes для L2
  schemas/
    schema_state.yaml     # active_version, last_bump_ts
    active/classification.json
    history/classification_v{N}.json
    proposals.yaml
    deprecated.yaml
  reports/L{level}_{ts}.json
  autopause.json
  signals.jsonl           # raw stream (если включён логгер)
```

`.cli-proxy/` целиком в `.gitignore` — runtime никогда не попадает в репозиторий.

## CLI (standalone)

```bash
# что в системе сейчас
python -m app.services.lint_evolution.cli --workdir . status

# собрать сигналы из manager review-файлов в SQLite
python -m app.services.lint_evolution.cli --workdir . ingest --project-root .

# регрессия весов (требует ≥200 outcomes total + ≥50/правило)
python -m app.services.lint_evolution.cli --workdir . run-l3

# canary: проверить рост fp_rate (может выставить autopause)
python -m app.services.lint_evolution.cli --workdir . canary

# снять паузу (после ручной диагностики)
python -m app.services.lint_evolution.cli --workdir . autopause-resume --level 2

# текущий schema-version и pending proposals
python -m app.services.lint_evolution.cli --workdir . schema-history
```

L1 и L2 запускаются программно (см. `evolver.run_level1`, `schema_evolver.run_level2`) — им нужен `classify_fn`/`meta_classify_fn`, делегирующий вызов активному CLI сессии.

## Trigger (для интеграции в bot)

`trigger.maybe_run_evolution(workdir, project_root, config, spawn, classify_fn, meta_classify_fn)` — единая точка вызова при активности сессии. Она:

1. Прогоняет canary (может включить autopause).
2. Для каждого уровня проверяет `cooldown / autopause / signals_count_since_last_run / lock` и, если всё ОК, отдаёт `spawn(coroutine)` — fire-and-forget через `task_service.create()`.

Cooldowns по умолчанию: L1 = 24h, L2 = 30d, L3 = 30d. Lock TTL = 30 минут. Error-retry = 1h.

## Защита от деградации

- **Temporal cross-validation** (`temporal_xval.classify_stable`): high-impact решения L1/L2 (APPLY, MERGE, EXTEND_SCHEMA) требуют двух независимых классификаций активного CLI; расхождение по критическим полям → автоматический HOLD.
- **Hard gates** перед score-функцией: исключают subjective / style / llm_only / low-evidence ветки.
- **Drift cap** в L3: если предложенный сдвиг весов > `max_weight_drift` (по умолчанию 0.5) — статус `paused_drift`, веса не пишутся.
- **Canary** (`canary_metric.evaluate`): рост global fp_rate week-over-week > 50% → autopause L1+L2; > 3 schema bumps за 180d → autopause L2.
- **Autopause** (`autopause.pause`): глобальный флаг, снимается **только** через CLI `autopause-resume` — единственная точка обязательного human-in-the-loop.

## Структура каталога модуля

| Файл | Назначение |
|---|---|
| `paths.py` | пути ко всем runtime-артефактам |
| `state.py` | per-project_id JSON state с lock/cooldown/error-retry |
| `fingerprints.py` | SQLite: `signals`, `runs`, `outcomes`, `schema_versions` |
| `signals_ingestor.py` | парсер `.manager/.../*review_result*.md` |
| `canonicalizer.py` | regex → canonical `rule_kind` |
| `rule_kinds.py` | 10 канонических kinds + `__unknown__` |
| `cli_classifier.py` | обёртка вокруг активного CLI: text → JSON по схеме |
| `temporal_xval.py` | двукратный classify, HOLD при расхождении |
| `lint_decision.py` | L1: hard gates + score → APPLY/MERGE/REVISE/HOLD/REJECT |
| `evolver.py` | L1 orchestrator |
| `rules_store.py` | `rules/self.yaml` lifecycle |
| `gate_service.py` | non-blocking pre-push lint gate |
| `outcomes_collector.py` | committed/reverted/ignored → SQLite |
| `candidates_store.py` | pending/rejected кандидаты |
| `schemas/__init__.py` + `classification_v1.json` + `decision_weights.yaml` | bundled templates |
| `schema_store.py` | versioned classification.json + proposals/deprecated |
| `meta_curator.py` | L2 prompt: notes → emergent_field |
| `schema_decision.py` | L2 hard gates + score → EXTEND/PROPOSE/DEFER/HOLD/REJECT |
| `schema_evolver.py` | L2 orchestrator |
| `weights_store.py` | `decision_weights.yaml` + `weights_history.yaml` |
| `weights_regressor.py` | L3: heuristic update + drift guard |
| `canary_metric.py` | global fp-rate / schema-thrash detection |
| `autopause.py` | per-level pause flag |
| `trigger.py` | единая точка для всех трёх уровней |
| `cli.py` | standalone CLI |
| `reports.py` | JSON отчёты прогонов |

## Тесты

`tests/test_lint_evolution_*.py` — 120 тестов покрывают canonicalizer, state, fingerprints, signals_ingestor, lint_decision, temporal_xval, cli_classifier, rules_store, gate, evolver (L1), schema_store, schema_decision, schema_evolver (L2), weights_store, weights_regressor (L3), autopause, canary, trigger.

```bash
python -m pytest tests/ -k "lint_evolution" -v
```
