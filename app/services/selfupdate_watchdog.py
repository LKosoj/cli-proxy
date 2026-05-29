import argparse
import json
import os
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional


def _read_marker(path: str) -> Optional[Dict[str, Any]]:
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _send_telegram_message(bot_token: str, *, chat_id: int, text: str) -> bool:
    token = str(bot_token or "").strip()
    if not token:
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = urllib.parse.urlencode({"chat_id": int(chat_id), "text": str(text or "")}).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return 200 <= int(getattr(resp, "status", 0) or 0) < 300
    except Exception:
        return False


def _remove_marker(path: str) -> None:
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Selfupdate timeout watchdog")
    parser.add_argument("--marker-path", required=True)
    parser.add_argument("--bot-token", required=True)
    parser.add_argument("--timeout-sec", type=int, default=30)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    marker_path = str(args.marker_path or "")
    timeout_sec = max(1, int(args.timeout_sec or 30))
    time.sleep(timeout_sec)

    marker = _read_marker(marker_path)
    if not marker:
        return 0

    try:
        chat_id = int(marker.get("chat_id"))
    except Exception:
        return 0
    if chat_id <= 0:
        return 0

    service_name = str(marker.get("service_name") or "").strip() or "bot.service"
    text = (
        f"Selfupdate: не удалось подтвердить перезапуск сервиса {service_name} "
        f"в течение {timeout_sec}с."
    )
    if _send_telegram_message(str(args.bot_token or ""), chat_id=chat_id, text=text):
        _remove_marker(marker_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
