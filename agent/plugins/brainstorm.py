"""
Плагин мозгового штурма с использованием различных методологий.
Вдохновлен проектом Brainstormers: https://github.com/Azzedde/brainstormers

Архитектура:
- Модель и температура задаются в определении каждой методологии
- Методологии выполняются параллельно (asyncio.gather)
- Синтез результатов выполняется большой моделью
- Две доступные модели равномерно распределены между методологиями
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from agent.openai_client import create_async_openai_client
from agent.plugins.base import ToolPlugin
from agent.tooling.spec import ToolSpec

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Методологии мозгового штурма
# ---------------------------------------------------------------------------
# model_type: "standard" = openai_model, "big" = big_model_to_use / openai_big_model
# Распределение: 3 методологии на standard, 3 на big — равномерно.

BRAINSTORM_METHODS: Dict[str, Dict[str, Any]] = {
    "big_mind_mapping": {
        "name": "Big Mind Mapping",
        "description": "Расширение идей по широкому спектру для максимальной генерации",
        "model_type": "big",
        "temperature": 0.8,
        "system_prompt": (
            "Вы эксперт по методологии Big Mind Mapping (карты разума).\n"
            "Ваша задача — создать широкую карту идей с множественными ветвями и под-идеями.\n\n"
            "Формат ответа:\n"
            "1. Основная тема\n"
            "2. Главные ветви (5-7 основных направлений)\n"
            "3. Под-идеи для каждой ветви (3-5 под-идей)\n"
            "4. Связи между ветвями\n\n"
            "Будьте креативны и исследуйте максимально широкий спектр возможностей."
        ),
    },
    "reverse_brainstorming": {
        "name": "Reverse Brainstorming",
        "description": "Определение потенциальных проблем для выявления инновационных решений",
        "model_type": "standard",
        "temperature": 0.7,
        "system_prompt": (
            "Вы эксперт по методологии Reverse Brainstorming (обратный мозговой штурм).\n"
            "Ваша задача — сначала определить способы УСУГУБИТЬ проблему, затем инвертировать их в решения.\n\n"
            "Формат ответа:\n"
            "1. Анализ проблемы\n"
            "2. Способы усугубить проблему (5-7 способов)\n"
            "3. Инверсия каждого способа в конструктивное решение\n"
            "4. Приоритизация решений по эффективности\n\n"
            "Будьте провокационны в определении проблем и креативны в их инверсии."
        ),
    },
    "role_storming": {
        "name": "Role Storming",
        "description": "Принятие различных персон для получения разнообразных инсайтов",
        "model_type": "big",
        "temperature": 0.85,
        "system_prompt": (
            "Вы эксперт по методологии Role Storming (ролевой мозговой штурм).\n"
            "Ваша задача — рассмотреть тему с позиций разных ролей и персонажей.\n\n"
            "Формат ответа:\n"
            "1. Определите 5-7 релевантных ролей/персон\n"
            "2. Для каждой роли:\n"
            "   - Перспектива и ценности этой роли\n"
            "   - Уникальные инсайты с позиции роли\n"
            "   - Предложения и идеи от этой роли\n"
            "3. Синтез идей из всех ролей\n\n"
            "Будьте эмпатичны и глубоко погружайтесь в каждую роль."
        ),
    },
    "scamper": {
        "name": "SCAMPER",
        "description": "Систематический креативный подход (Substitute, Combine, Adapt, Modify, Put to another use, Eliminate, Reverse)",
        "model_type": "standard",
        "temperature": 0.75,
        "system_prompt": (
            "Вы эксперт по методологии SCAMPER — систематическому креативному мышлению.\n"
            "Примените 7 техник SCAMPER к теме.\n\n"
            "Формат ответа:\n"
            "1. **Substitute (Заменить)**: Что можно заменить?\n"
            "2. **Combine (Объединить)**: Что можно объединить?\n"
            "3. **Adapt (Адаптировать)**: Что можно адаптировать?\n"
            "4. **Modify (Модифицировать)**: Что можно изменить, увеличить или уменьшить?\n"
            "5. **Put to another use (Применить иначе)**: Как ещё можно использовать?\n"
            "6. **Eliminate (Устранить)**: Что можно удалить или упростить?\n"
            "7. **Reverse (Инвертировать)**: Что можно перевернуть или реорганизовать?\n\n"
            "Для каждой техники предложите 3-5 конкретных идей."
        ),
    },
    "six_thinking_hats": {
        "name": "Six Thinking Hats",
        "description": "Исследование идеи с шести различных углов (факты, эмоции, риски, выгоды, креативность, процесс)",
        "model_type": "big",
        "temperature": 0.6,
        "system_prompt": (
            "Вы эксперт по методологии Six Thinking Hats Эдварда де Боно.\n"
            "Проанализируйте тему с позиций шести шляп мышления.\n\n"
            "Формат ответа:\n"
            "1. **Белая шляпа (Факты)**: Объективные данные и информация\n"
            "2. **Красная шляпа (Эмоции)**: Интуиция, чувства, эмоциональная реакция\n"
            "3. **Чёрная шляпа (Риски)**: Осторожность, потенциальные проблемы, критика\n"
            "4. **Жёлтая шляпа (Выгоды)**: Оптимизм, преимущества, ценность\n"
            "5. **Зелёная шляпа (Креативность)**: Новые идеи, альтернативы, возможности\n"
            "6. **Синяя шляпа (Процесс)**: Контроль, организация, выводы\n\n"
            "Каждая шляпа должна содержать детальный анализ."
        ),
    },
    "starbursting": {
        "name": "Starbursting",
        "description": "Генерация всесторонних вопросов по методу 5W1H (Who, What, Where, When, Why, How)",
        "model_type": "standard",
        "temperature": 0.65,
        "system_prompt": (
            "Вы эксперт по методологии Starbursting — генерации всесторонних вопросов.\n"
            "Создайте звезду вопросов по методу 5W1H и ответьте на них.\n\n"
            "Формат ответа:\n"
            "1. **Who (Кто)**: 5-7 вопросов о людях/участниках + ответы\n"
            "2. **What (Что)**: 5-7 вопросов о сути/содержании + ответы\n"
            "3. **Where (Где)**: 5-7 вопросов о месте/контексте + ответы\n"
            "4. **When (Когда)**: 5-7 вопросов о времени/сроках + ответы\n"
            "5. **Why (Почему)**: 5-7 вопросов о причинах/целях + ответы\n"
            "6. **How (Как)**: 5-7 вопросов о методах/способах + ответы\n\n"
            "Вопросы должны быть глубокими, а ответы — практичными и конкретными."
        ),
    },
}

# Предустановленные наборы методологий
METHOD_PRESETS: Dict[str, List[str]] = {
    "all": list(BRAINSTORM_METHODS.keys()),
    "creative": ["big_mind_mapping", "scamper", "role_storming"],
    "analytical": ["six_thinking_hats", "starbursting"],
    "problem_solving": ["reverse_brainstorming", "scamper"],
}

# Системный промпт для синтеза
SYNTHESIS_SYSTEM_PROMPT = (
    "Вы — эксперт по синтезу идей и стратегическому мышлению.\n"
    "Ваша задача — создать наиболее ценный и практичный итоговый отчёт "
    "из результатов множественных методологий мозгового штурма.\n\n"
    "Принципы синтеза:\n"
    "- Объединяйте схожие идеи\n"
    "- Выделяйте уникальные инсайты\n"
    "- Приоритизируйте практичность\n"
    "- Структурируйте по категориям\n"
    "- Создавайте действенные рекомендации\n\n"
    "Формат: чёткий, структурированный, профессиональный на русском языке."
)


class BrainstormTool(ToolPlugin):
    """Плагин мозгового штурма с множественными методологиями и параллельным выполнением."""

    def get_spec(self) -> ToolSpec:
        methods_desc = ", ".join(
            f"{k} ({v['name']})" for k, v in BRAINSTORM_METHODS.items()
        )
        presets_desc = ", ".join(METHOD_PRESETS.keys())
        return ToolSpec(
            name="brainstorm",
            description=(
                "Multi-model brainstorming tool using diverse methodologies "
                "(Big Mind Mapping, Reverse Brainstorming, Role Storming, SCAMPER, "
                "Six Thinking Hats, Starbursting). Runs methods in parallel, then "
                "synthesizes results into a structured report. "
                "Use for creative ideation, problem solving, strategic analysis."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "description": "Topic or question for brainstorming",
                    },
                    "methods": {
                        "type": "string",
                        "description": (
                            f"Which methodologies to use. "
                            f"Presets: {presets_desc}. "
                            f"Or comma-separated method keys: {methods_desc}. "
                            f"Default: 'all'"
                        ),
                    },
                    "parallel": {
                        "type": "boolean",
                        "description": "Run methodologies in parallel (default: true)",
                    },
                },
                "required": ["topic"],
            },
            parallelizable=False,
            timeout_ms=600_000,  # 10 мин — долгий инструмент
        )

    # -----------------------------------------------------------------
    # Helpers: model & client
    # -----------------------------------------------------------------

    def _get_model(self, model_type: str) -> str:
        """Resolve model name from config. 'standard' or 'big'."""
        cfg = getattr(self, "config", None)
        defaults = getattr(cfg, "defaults", None) if cfg else None
        if model_type == "big":
            return (
                os.getenv("OPENAI_BIG_MODEL")
                or (getattr(defaults, "openai_big_model", None) if defaults else None)
                or (getattr(defaults, "big_model_to_use", None) if defaults else None)
                or "gpt-4o"
            )
        return (
            os.getenv("OPENAI_MODEL")
            or (getattr(defaults, "openai_model", None) if defaults else None)
            or "gpt-4o-mini"
        )

    def _get_client(self):
        cfg = getattr(self, "config", None)
        defaults = getattr(cfg, "defaults", None) if cfg else None
        api_key = (
            os.getenv("OPENAI_API_KEY")
            or (getattr(defaults, "openai_api_key", None) if defaults else None)
        )
        base_url = (
            os.getenv("OPENAI_BASE_URL")
            or (getattr(defaults, "openai_base_url", None) if defaults else None)
        )
        if not api_key:
            raise RuntimeError("OpenAI API key not configured")
        return create_async_openai_client(api_key=api_key, base_url=base_url or None)

    # -----------------------------------------------------------------
    # Single-method brainstorm
    # -----------------------------------------------------------------

    async def _run_method(self, topic: str, method_key: str) -> Dict[str, Any]:
        """Execute a single brainstorming methodology."""
        method = BRAINSTORM_METHODS[method_key]
        model_type = method.get("model_type", "standard")
        model = self._get_model(model_type)
        temperature = method.get("temperature", 0.8)
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        logger.info(
            "🧠 Brainstorm: %s | model=%s (type=%s) | temp=%.2f",
            method["name"], model, model_type, temperature,
        )

        user_prompt = (
            f"Тема для мозгового штурма: {topic}\n\n"
            f"Примените методологию {method['name']} для всестороннего анализа этой темы.\n"
            f"Будьте креативны, глубоки и практичны в своих идеях."
        )
        system_prompt = (
            method["system_prompt"]
            + f"\n\n*Текущие дата и время*: {now_str}"
        )

        try:
            client = self._get_client()
            resp = await client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=temperature,
                max_tokens=4000,
            )
            content = (resp.choices[0].message.content or "").strip() if resp.choices else ""
            logger.info("✅ %s: %d chars", method["name"], len(content))
            return {
                "method": method["name"],
                "method_key": method_key,
                "description": method["description"],
                "model": model,
                "model_type": model_type,
                "temperature": temperature,
                "content": content,
                "success": True,
            }
        except Exception as e:
            logger.error("❌ %s: %s", method["name"], e)
            return {
                "method": method["name"],
                "method_key": method_key,
                "description": method["description"],
                "model": model,
                "model_type": model_type,
                "temperature": temperature,
                "content": f"Ошибка выполнения: {e}",
                "success": False,
                "error": str(e),
            }

    # -----------------------------------------------------------------
    # Synthesis
    # -----------------------------------------------------------------

    async def _synthesize(self, topic: str, results: List[Dict[str, Any]]) -> str:
        """Combine all method results into a single report using the big model."""
        successful = [r for r in results if r["success"]]
        if not successful:
            return "Не удалось получить результаты ни от одной методологии."

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        parts = [f"Тема мозгового штурма: {topic}\n\n"
                 "Ниже представлены результаты мозгового штурма по различным методологиям.\n"
                 "Ваша задача — синтезировать все идеи в единый, структурированный и практичный отчёт.\n\n"]

        for i, r in enumerate(successful, 1):
            parts.append(
                f"{'=' * 80}\n"
                f"МЕТОДОЛОГИЯ {i}: {r['method']}\n"
                f"Модель: {r['model']}\n"
                f"Описание: {r['description']}\n"
                f"{'=' * 80}\n\n"
                f"{r['content']}\n\n"
            )

        parts.append(
            "Теперь создайте ИТОГОВЫЙ СИНТЕЗИРОВАННЫЙ ОТЧЁТ:\n\n"
            "1. **Исполнительное резюме** (ключевые инсайты из всех методологий)\n"
            "2. **Топ-10 лучших идей** (самые ценные идеи со всех подходов)\n"
            "3. **Анализ по категориям**:\n"
            "   - Стратегические решения\n"
            "   - Тактические решения\n"
            "   - Инновационные подходы\n"
            "   - Риски и ограничения\n"
            "4. **План действий** (приоритизированные шаги)\n"
            "5. **Рекомендации**\n"
            "6. **Матрица решений** (сравнительный анализ идей)\n\n"
            "Синтезируйте идеи из ВСЕХ методологий в единое целое.\n"
            "Выделяйте наиболее ценные и практичные решения.\n"
            "Устраняйте дубликаты и объединяйте схожие идеи.\n"
            "Создайте практичный, структурированный и действенный документ."
        )

        synthesis_prompt = "".join(parts)
        system = SYNTHESIS_SYSTEM_PROMPT + f"\n\n*Текущие дата и время*: {now_str}"
        model = self._get_model()

        logger.info("🎨 Synthesis with model=%s, prompt_len=%d", model, len(synthesis_prompt))

        try:
            client = self._get_client()
            resp = await client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": synthesis_prompt},
                ],
                temperature=0.6,
                max_tokens=12000,
            )
            report = (resp.choices[0].message.content or "").strip() if resp.choices else ""
            if not report:
                raise RuntimeError("Model returned empty response")
            logger.info("✅ Synthesis done: %d chars", len(report))
            return report
        except Exception as e:
            logger.error("❌ Synthesis error (big model): %s — trying standard model", e)
            # Fallback: стандартная модель
            try:
                fallback_model = self._get_model("standard")
                client = self._get_client()
                resp = await client.chat.completions.create(
                    model=fallback_model,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": synthesis_prompt},
                    ],
                    temperature=0.6,
                    max_tokens=12000,
                )
                report = (resp.choices[0].message.content or "").strip() if resp.choices else ""
                if report:
                    logger.info("✅ Synthesis done (fallback): %d chars", len(report))
                    return report
            except Exception as e2:
                logger.error("❌ Synthesis fallback error: %s", e2)

            # Если и фолбэк не сработал — возвращаем сырые результаты
            raw_parts = ["⚠️ Не удалось синтезировать результаты. Сырые данные:\n"]
            for r in successful:
                raw_parts.append(f"\n{'=' * 60}\n{r['method']}\n{'=' * 60}\n\n{r['content']}\n")
            return "".join(raw_parts)

    # -----------------------------------------------------------------
    # File saving
    # -----------------------------------------------------------------

    def _save_report(self, content: str, state_root: str) -> Optional[str]:
        """Save the report to a markdown file and return the path."""
        try:
            ts = time.strftime("%Y%m%d_%H%M%S")
            filename = f"brainstorm_{ts}.md"
            filepath = os.path.join(state_root, filename)
            os.makedirs(os.path.dirname(filepath) or ".", exist_ok=True)
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(content)
            logger.info("📄 Report saved: %s", filepath)
            return filepath
        except Exception as e:
            logger.warning("Failed to save brainstorm report: %s", e)
            return None

    # -----------------------------------------------------------------
    # execute (agent API)
    # -----------------------------------------------------------------

    async def execute(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        topic = (args.get("topic") or "").strip()
        if not topic:
            return {"success": False, "error": "Topic is required"}

        methods_arg = (args.get("methods") or "all").strip()
        parallel = args.get("parallel", True)
        if parallel is None:
            parallel = True

        # Resolve method list
        if methods_arg in METHOD_PRESETS:
            selected = METHOD_PRESETS[methods_arg]
        else:
            selected = [m.strip() for m in methods_arg.split(",")]
            invalid = [m for m in selected if m not in BRAINSTORM_METHODS]
            if invalid:
                available = list(BRAINSTORM_METHODS.keys())
                return {
                    "success": False,
                    "error": f"Unknown methods: {invalid}. Available: {available}",
                }

        if not selected:
            return {"success": False, "error": "No valid methods selected"}

        logger.info(
            "🎯 Brainstorm start | topic='%s' | methods=%s | parallel=%s",
            topic[:100], selected, parallel,
        )

        # Log model distribution
        for mk in selected:
            m = BRAINSTORM_METHODS[mk]
            logger.info(
                "   - %s: %s (temp=%.2f)",
                m["name"], self._get_model(m.get("model_type", "standard")),
                m.get("temperature", 0.8),
            )

        # Run methodologies
        if parallel and len(selected) > 1:
            logger.info("⚡ Running %d methods in parallel...", len(selected))
            tasks = [self._run_method(topic, mk) for mk in selected]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            # Convert exceptions to error dicts
            clean_results: List[Dict[str, Any]] = []
            for i, r in enumerate(results):
                if isinstance(r, Exception):
                    mk = selected[i]
                    clean_results.append({
                        "method": BRAINSTORM_METHODS[mk]["name"],
                        "method_key": mk,
                        "description": BRAINSTORM_METHODS[mk]["description"],
                        "model": "unknown",
                        "model_type": "unknown",
                        "temperature": 0,
                        "content": f"Ошибка: {r}",
                        "success": False,
                        "error": str(r),
                    })
                else:
                    clean_results.append(r)
            results_list = clean_results
        else:
            logger.info("🔄 Running %d methods sequentially...", len(selected))
            results_list = []
            for mk in selected:
                result = await self._run_method(topic, mk)
                results_list.append(result)

        success_count = sum(1 for r in results_list if r["success"])
        logger.info(
            "✅ All methods done. Successful: %d/%d",
            success_count, len(results_list),
        )

        # Synthesize
        logger.info("🎨 Starting synthesis...")
        report = await self._synthesize(topic, results_list)

        # Build metadata header
        meta_lines = [
            f"{'=' * 80}",
            "МЕТА-ИНФОРМАЦИЯ О МОЗГОВОМ ШТУРМЕ",
            f"{'=' * 80}",
            "",
            f"Тема: {topic}",
            f"Дата: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"Использовано методологий: {len(results_list)}",
            f"Успешных результатов: {success_count}",
            "Применённые методологии:",
        ]
        for r in results_list:
            status = "✅" if r["success"] else "❌"
            meta_lines.append(f"  {status} {r['method']} ({r.get('model', 'unknown')})")
        meta_lines.append("")

        metadata = "\n".join(meta_lines)
        full_report = f"{metadata}\n{report}"

        # Save to file
        state_root = ctx.get("state_root") or ctx.get("cwd") or "."
        saved_path = self._save_report(full_report, state_root)
        if saved_path:
            full_report += f"\n\n📄 Файл с результатами: {saved_path}"

        return {"success": True, "output": full_report}
