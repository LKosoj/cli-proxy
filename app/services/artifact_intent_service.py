"""Lightweight LLM-based classifier for artifact delivery requests.

When a user explicitly asks to receive a project file via Telegram
(e.g. "пришли мне config.yaml", "скинь package.json"), this service
detects the intent and resolves the file path — so the bot can send
the document directly without launching a full CLI agent session.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from modes.sdk.runtime.json_normalizer import loads_safe

_LOG = logging.getLogger(__name__)

_MIN_CONFIDENCE = 0.7

_SYSTEM_PROMPT = (
    "Ты классификатор намерений пользователя. "
    "Определи, просит ли пользователь ЯВНО отправить/прислать/скинуть ему конкретный файл из проекта. "
    "Верни JSON: {\"is_artifact_request\": bool, \"file_pattern\": string, \"confidence\": number 0..1}.\n\n"
    "Правила:\n"
    "- is_artifact_request=true ТОЛЬКО если пользователь явно просит ПРИСЛАТЬ/ОТПРАВИТЬ/СКИНУТЬ файл.\n"
    "- НЕ срабатывай на: \"посмотри файл\", \"открой\", \"покажи содержимое\", \"измени\", "
    "\"что в файле\", \"прочитай\" — это задачи для CLI.\n"
    "- Срабатывай на: \"пришли\", \"скинь\", \"отправь файл\", \"дай файл\", \"кинь\", "
    "\"перешли\", \"загрузи мне\", \"скачать\".\n"
    "- file_pattern — путь к файлу как указал пользователь (относительный или абсолютный, если пользователь дал полный путь).\n"
    "- Если пользователь не просит прислать файл, верни is_artifact_request=false, "
    "file_pattern=\"\", confidence=1.0."
)

# Files/patterns that must never be sent.
_BLOCKED_PATTERNS = {
    ".env",
    ".env.local",
    ".env.production",
    "credentials.json",
    "secrets.yaml",
    "secrets.yml",
    "id_rsa",
    "id_ed25519",
}

TELEGRAM_FILE_SIZE_LIMIT = 50 * 1024 * 1024  # 50 MB


@dataclass(frozen=True)
class ArtifactIntent:
    file_pattern: str
    confidence: float


@dataclass(frozen=True)
class ArtifactResult:
    resolved_path: str
    error: Optional[str] = None


class ArtifactIntentService:
    """Detects 'send me a file' intent via a lightweight LLM call."""

    async def classify(
        self,
        text: str,
        *,
        app_config: Any,
        llm_fn: Callable,
        model: Optional[str] = None,
    ) -> Optional[ArtifactIntent]:
        """Return ArtifactIntent if user explicitly asks to receive a file, else None."""
        if not text or not text.strip():
            return None

        try:
            raw = await llm_fn(
                app_config,
                _SYSTEM_PROMPT,
                text,
                response_format={"type": "json_object"},
                model=model,
                max_tokens=8000,
            )
            payload = loads_safe(str(raw or "").strip(), strict_first=False)
        except Exception:
            _LOG.exception("artifact intent classification failed")
            return None

        is_artifact = payload.get("is_artifact_request", False)
        if not is_artifact:
            return None

        file_pattern = str(payload.get("file_pattern") or "").strip()
        if not file_pattern:
            return None

        try:
            confidence = float(payload.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))

        if confidence < _MIN_CONFIDENCE:
            return None

        return ArtifactIntent(file_pattern=file_pattern, confidence=confidence)

    def resolve(
        self,
        intent: ArtifactIntent,
        project_root: Optional[str],
    ) -> ArtifactResult:
        """Resolve file_pattern to an absolute path with safety checks."""
        if not project_root:
            return ArtifactResult(resolved_path="", error="Не задан project_root сессии.")

        root = Path(project_root).resolve()
        if not root.is_dir():
            return ArtifactResult(resolved_path="", error=f"Директория проекта не найдена: {root}")

        candidate = (root / intent.file_pattern).resolve()

        # Safety: must be inside project_root.
        try:
            candidate.relative_to(root)
        except ValueError:
            return ArtifactResult(
                resolved_path="",
                error="Запрошенный путь выходит за пределы проекта.",
            )

        # Check blocked patterns.
        basename = candidate.name.lower()
        for blocked in _BLOCKED_PATTERNS:
            if basename == blocked.lower():
                return ArtifactResult(
                    resolved_path="",
                    error=f"Файл {candidate.name} содержит секреты и не может быть отправлен.",
                )

        if not candidate.is_file():
            # Try glob in project root.
            matches = sorted(root.glob(intent.file_pattern))
            matches = [m for m in matches if m.is_file()]
            if not matches:
                return ArtifactResult(
                    resolved_path="",
                    error=f"Файл не найден: {intent.file_pattern}",
                )
            if len(matches) > 1:
                listing = "\n".join(f"• {m.relative_to(root)}" for m in matches[:10])
                suffix = f"\n…и ещё {len(matches) - 10}" if len(matches) > 10 else ""
                return ArtifactResult(
                    resolved_path="",
                    error=f"Найдено несколько файлов:\n{listing}{suffix}\nУточните, какой именно.",
                )
            candidate = matches[0]
            # Re-check safety for glob result.
            try:
                candidate.relative_to(root)
            except ValueError:
                return ArtifactResult(
                    resolved_path="",
                    error="Результат поиска выходит за пределы проекта.",
                )
            basename = candidate.name.lower()
            for blocked in _BLOCKED_PATTERNS:
                if basename == blocked.lower():
                    return ArtifactResult(
                        resolved_path="",
                        error=f"Файл {candidate.name} содержит секреты и не может быть отправлен.",
                    )

        # Size check.
        try:
            size = candidate.stat().st_size
        except OSError:
            return ArtifactResult(resolved_path="", error="Не удалось прочитать файл.")

        if size > TELEGRAM_FILE_SIZE_LIMIT:
            mb = size / (1024 * 1024)
            return ArtifactResult(
                resolved_path="",
                error=f"Файл слишком большой ({mb:.1f} МБ). Лимит Telegram — 50 МБ.",
            )

        return ArtifactResult(resolved_path=str(candidate))
