from __future__ import annotations

import re
from typing import Any, Dict, Optional

from config import AppConfig
from .json_normalizer import loads_safe
from .memory_store import parse_entries
from .openai_client import chat_completion


_DECIDER_SYSTEM = """Ты принимаешь решение, что сохранить в долговременную память проекта.
Сохраняй только устойчивые и полезные факты.
Используй категории:
- preference, decision, config, agreement (слой semantic, без TTL)
- task_state (слой task_state, TTL 14 дней)
Приоритет категорий: preference (самый важный) > decision > config > agreement > task_state.
Не сохраняй личные данные, чувствительную информацию и временные детали.
Запись должна быть атомарной: одно короткое утверждение, без списков и переносов строк.
Верни строго JSON:
{
  "save": true/false,
  "category": "preference|decision|config|agreement|task_state",
  "layer": "semantic|task_state",
  "content": "короткая запись (1-2 предложения)",
  "source": "agent|user|system",
  "confidence": 0.0-1.0,
  "ttl_days": 14,
  "verification_status": "verified|unverified",
  "evidence_type": "user|tool|code|config|system|none",
  "evidence_ref": "короткая ссылка на подтверждение, если есть"
}
Semantic-память (preference/decision/config/agreement) можно сохранять только как verified
и только с evidence_type не none. Если факт является выводом агента без подтверждения
инструментом, кодом, конфигом, системой или прямым текстом пользователя — не сохраняй его в semantic.
"""


_COMPRESS_SYSTEM = """Ты сжимаешь память проекта, чтобы она помещалась в заданный лимит.
Приоритет сохранения: preference > decision > config > agreement.
Удаляй повторы и мелкие детали. Сохраняй только устойчивые факты.
Сохраняй существующие trust-токены [VER:*] [EVID:*] [REF:*] и не повышай уровень проверки.
Сохраняй формат строк: "- YYYY-MM-DD HH:MM: [TAG] [LAYER:*] [SRC:*] [ID:*] [VER:*] [EVID:*] текст".
Верни только сжатый текст памяти без JSON."""

_ALLOWED_VERIFICATION_STATUSES = {"verified", "unverified"}
_ALLOWED_EVIDENCE_TYPES = {"user", "tool", "code", "config", "system", "none"}
_MEMORY_TOKEN_RE = re.compile(r"\[([A-Za-z]+):([^\]]*)\]")


def _normalize_verification_status(value: Any, *, default: str = "unverified") -> str:
    status = str(value or "").strip().lower()
    if status in _ALLOWED_VERIFICATION_STATUSES:
        return status
    return default


def _normalize_evidence_type(value: Any, *, default: str = "none") -> str:
    evidence_type = str(value or "").strip().lower()
    if evidence_type in _ALLOWED_EVIDENCE_TYPES:
        return evidence_type
    return default


def _entries_by_id_or_text(memory_text: str) -> Dict[str, Dict[str, Any]]:
    indexed: Dict[str, Dict[str, Any]] = {}
    for entry in parse_entries(memory_text or ""):
        entry_id = str(entry.get("id") or "").strip()
        key = entry_id or f"{entry.get('tag')}:{entry.get('text')}"
        if key:
            indexed[key] = entry
    return indexed


def _raw_trust_tokens_by_id(memory_text: str) -> Dict[str, Dict[str, list[str]]]:
    by_id: Dict[str, Dict[str, list[str]]] = {}
    for line in (memory_text or "").splitlines():
        tokens: Dict[str, list[str]] = {}
        for key, value in _MEMORY_TOKEN_RE.findall(line):
            normalized_key = str(key or "").strip().upper()
            if normalized_key in {"ID", "VER", "EVID", "REF"}:
                tokens.setdefault(normalized_key, []).append(str(value or "").strip())
        entry_ids = tokens.get("ID") or []
        if entry_ids:
            by_id[entry_ids[0]] = tokens
    return by_id


def _compression_preserves_trust(original_text: str, compressed_text: str) -> bool:
    original = _entries_by_id_or_text(original_text)
    compressed_entries = parse_entries(compressed_text or "")
    raw_tokens_by_id = _raw_trust_tokens_by_id(compressed_text)
    raw_verified_count = str(compressed_text or "").lower().count("[ver:verified]")
    parsed_verified_ids = {
        str(entry.get("id") or "").strip()
        for entry in compressed_entries
        if str(entry.get("verification_status") or "").strip().lower() == "verified"
        and str(entry.get("id") or "").strip()
    }
    original_verified_ids = {
        str(entry.get("id") or "").strip()
        for entry in original.values()
        if str(entry.get("verification_status") or "").strip().lower() == "verified"
        and str(entry.get("id") or "").strip()
    }
    if raw_verified_count != len(parsed_verified_ids):
        return False
    if not parsed_verified_ids.issubset(original_verified_ids):
        return False
    for entry in compressed_entries:
        entry_id = str(entry.get("id") or "").strip()
        key = entry_id or f"{entry.get('tag')}:{entry.get('text')}"
        source = original.get(key)
        next_ver = str(entry.get("verification_status") or "legacy").strip().lower()
        next_evid = str(entry.get("evidence_type") or "legacy").strip().lower()
        if source is None:
            if next_ver == "verified":
                return False
            continue
        source_ver = str(source.get("verification_status") or "legacy").strip().lower()
        source_evid = str(source.get("evidence_type") or "legacy").strip().lower()
        source_ref = str(source.get("evidence_ref") or "").strip()
        raw_tokens = raw_tokens_by_id.get(str(entry.get("id") or "").strip(), {})
        if source_ver != "verified" and next_ver == "verified":
            return False
        if next_ver == "verified" and next_evid in ("", "none", "legacy"):
            return False
        if source_ver == "verified":
            if (
                len(raw_tokens.get("VER") or []) != 1
                or len(raw_tokens.get("EVID") or []) != 1
                or len(raw_tokens.get("REF") or []) > 1
            ):
                return False
            if (raw_tokens.get("VER") or [""])[0].strip().lower() != "verified":
                return False
            if (raw_tokens.get("EVID") or [""])[0].strip().lower() != source_evid:
                return False
            raw_refs = raw_tokens.get("REF") or []
            if (raw_refs[0].strip() if raw_refs else "") != source_ref:
                return False
            if next_ver != "verified" or next_evid != source_evid:
                return False
            if str(entry.get("text") or "") != str(source.get("text") or ""):
                return False
            if str(entry.get("evidence_ref") or "").strip() != source_ref:
                return False
    return True


async def decide_memory_save(
    config: AppConfig, user_text: str, final_response: str, memory_text: str
) -> Optional[Dict[str, Any]]:
    prompt = (
        f"Текущая память:\n{memory_text}\n\n"
        f"Запрос пользователя:\n{user_text}\n\n"
        f"Итоговый ответ:\n{final_response}\n\n"
        "Нужно ли сохранять что-то новое?"
    )
    raw = await chat_completion(config, _DECIDER_SYSTEM, prompt, response_format={"type": "json_object"})
    if not raw:
        return None
    try:
        payload = loads_safe(raw, strict_first=False)
    except Exception:
        return None
    if not payload.get("save"):
        return None
    content = (payload.get("content") or "").strip()
    category = (payload.get("category") or "").strip().lower()
    layer = (payload.get("layer") or "").strip().lower()
    source = (payload.get("source") or "").strip().lower() or "agent"
    verification_status = _normalize_verification_status(payload.get("verification_status"))
    evidence_type = _normalize_evidence_type(payload.get("evidence_type"))
    evidence_ref = " ".join(str(payload.get("evidence_ref") or "").replace("\n", " ").split())[:120]
    confidence_raw = payload.get("confidence")
    ttl_days_raw = payload.get("ttl_days")
    if not content:
        return None
    if category not in ("preference", "decision", "config", "agreement", "task_state"):
        return None
    if category == "task_state":
        layer = "task_state"
    if layer not in ("semantic", "task_state"):
        layer = "semantic"
    if evidence_type == "user":
        return None
    if layer == "semantic" and (verification_status != "verified" or evidence_type == "none"):
        return None
    if layer == "task_state" and verification_status == "verified" and evidence_type == "none":
        verification_status = "unverified"
    try:
        confidence = float(confidence_raw)
    except Exception:
        confidence = 0.8 if layer == "semantic" else 0.6
    confidence = max(0.0, min(1.0, confidence))
    try:
        ttl_days = int(ttl_days_raw) if ttl_days_raw is not None else None
    except Exception:
        ttl_days = None
    if layer == "task_state" and (ttl_days is None or ttl_days <= 0):
        ttl_days = 14
    if layer != "task_state":
        ttl_days = None
    tag = {
        "preference": "PREF",
        "decision": "DECISION",
        "config": "CONFIG",
        "agreement": "AGREEMENT",
        "task_state": "TASK",
    }[category]
    return {
        "tag": tag,
        "content": content,
        "layer": layer,
        "source": source if source in ("agent", "user", "system") else "agent",
        "confidence": confidence,
        "ttl_days": ttl_days,
        "verification_status": verification_status,
        "evidence_type": evidence_type,
        "evidence_ref": evidence_ref,
    }


async def compress_memory(config: AppConfig, memory_text: str, max_chars: int) -> Optional[str]:
    if not memory_text:
        return ""
    prompt = f"Лимит: {max_chars} символов.\n\nПамять:\n{memory_text}"
    raw = await chat_completion(config, _COMPRESS_SYSTEM, prompt)
    compressed = raw.strip() if raw else None
    if compressed and "[VER:verified]" in memory_text and "[VER:verified]" not in compressed:
        return None
    if compressed and not _compression_preserves_trust(memory_text, compressed):
        return None
    return compressed
