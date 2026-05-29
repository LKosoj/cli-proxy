import hashlib
import hmac
import time
from dataclasses import dataclass
from typing import Any, Dict
from urllib.parse import parse_qsl, unquote

from modes.sdk.runtime.json_normalizer import loads_safe


class MiniAppAuthError(Exception):
    """Authentication failed for Telegram WebApp initData."""


@dataclass
class TelegramMiniAppUser:
    user_id: int
    username: str
    first_name: str


def verify_telegram_init_data(init_data: str, bot_token: str, max_age_sec: int = 86_400) -> TelegramMiniAppUser:
    if not init_data or not bot_token:
        raise MiniAppAuthError("missing initData or bot token")

    parsed: Dict[str, str] = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = parsed.pop("hash", None)
    if not received_hash:
        raise MiniAppAuthError("initData hash is missing")

    auth_date_raw = parsed.get("auth_date")
    if not auth_date_raw:
        raise MiniAppAuthError("auth_date is missing")

    try:
        auth_ts = int(auth_date_raw)
    except ValueError as exc:
        raise MiniAppAuthError("invalid auth_date") from exc

    if int(time.time()) - auth_ts > int(max_age_sec):
        raise MiniAppAuthError("initData expired")

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    expected_hash = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(received_hash, expected_hash):
        raise MiniAppAuthError("signature mismatch")

    user_raw = parsed.get("user")
    if not user_raw:
        raise MiniAppAuthError("user payload is missing")

    try:
        user_obj: Dict[str, Any] = loads_safe(unquote(user_raw), strict_first=True)
    except Exception as exc:
        raise MiniAppAuthError("invalid user payload") from exc

    user_id = user_obj.get("id")
    if not isinstance(user_id, int):
        raise MiniAppAuthError("invalid user id")

    return TelegramMiniAppUser(
        user_id=user_id,
        username=str(user_obj.get("username") or ""),
        first_name=str(user_obj.get("first_name") or ""),
    )
