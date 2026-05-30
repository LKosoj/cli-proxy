from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict, Optional


_RETRYABLE_TEXT_PATTERNS = (
    "api error:",
    "[api error:",
    "quota exceeded",
    "api quota has been exhausted",
    "rate limit",
    "429",
    "temporarily unavailable",
    "try again later",
    "connection reset",
    "connection refused",
    "timed out",
    "request timed out",
    "overloaded",
    "503 service unavailable",
)


def classify_retryable_cli_text(text: str) -> str:
    raw = str(text or "").strip()
    if not raw:
        return ""
    lowered = raw.lower()
    if any(pattern in lowered for pattern in _RETRYABLE_TEXT_PATTERNS):
        return raw.splitlines()[0].strip()[:300]
    return ""


def classify_retryable_cli_exception(exc: Exception) -> str:
    return classify_retryable_cli_text(str(exc or ""))


async def run_cli_with_retry(
    invoke: Callable[[], Awaitable[str]],
    *,
    max_attempts: int = 2,
) -> Dict[str, Any]:
    attempts = max(1, int(max_attempts))
    last_exc: Optional[Exception] = None
    for attempt in range(1, attempts + 1):
        try:
            output = await invoke()
        except Exception as exc:
            last_exc = exc
            retry_reason = classify_retryable_cli_exception(exc)
            if retry_reason and attempt < attempts:
                continue
            raise
        retry_reason = classify_retryable_cli_text(output)
        if retry_reason and attempt < attempts:
            continue
        return {
            "output": output,
            "attempts": attempt,
            "retried": attempt > 1,
            "retry_reason": retry_reason,
            "retry_exhausted": bool(retry_reason and attempt >= attempts),
        }
    if last_exc is not None:
        raise last_exc
    return {
        "output": "",
        "attempts": attempts,
        "retried": attempts > 1,
        "retry_reason": "",
        "retry_exhausted": False,
    }
