from __future__ import annotations

import logging
import os
import time
from typing import Dict, Optional

import yaml

from modes.sdk.json_store import read_json_locked, write_json_locked

from .feedback_optimizer import normalize_learning_payload
from .models import WebmasterContext

_log = logging.getLogger(__name__)


def build_user_key(chat_id: int | None, user_id: int | None, session_id: Optional[str] = None) -> str:
    c = int(chat_id or 0)
    u = int(user_id or 0)
    if u <= 0 and c > 0:
        u = c
    sid = str(session_id or "").strip()
    if not sid or sid == "0":
        return f"{c}_{u}"
    safe_sid = sid.replace("/", "_").replace("\\", "_").strip() or "0"
    return f"{c}_{u}_{safe_sid}"


class WebmasterStateStore:
    def __init__(self, state_root: str, *, prompt_learning_path: Optional[str] = None) -> None:
        root = os.path.abspath(str(state_root or "."))
        self.root_dir = root
        self.users_dir = os.path.join(root, "users")
        if prompt_learning_path:
            self.prompt_learning_path = os.path.abspath(str(prompt_learning_path))
        else:
            self.prompt_learning_path = os.path.join(root, "learning.yaml")
        os.makedirs(self.users_dir, exist_ok=True)
        os.makedirs(os.path.dirname(self.prompt_learning_path), exist_ok=True)

    def path_for(self, user_key: str) -> str:
        safe = (str(user_key or "0_0").replace("/", "_").replace("\\", "_")).strip() or "0_0"
        return os.path.join(self.users_dir, f"{safe}.json")

    def load(self, user_key: str) -> WebmasterContext:
        key = str(user_key or "").strip() or "0_0"
        path = self.path_for(key)
        raw = read_json_locked(path, default={})
        if not isinstance(raw, dict):
            raw = {}
        return WebmasterContext.from_dict(raw, key)

    def save(self, ctx: WebmasterContext) -> None:
        ctx.updated_at = time.time()
        path = self.path_for(ctx.key)
        write_json_locked(path, ctx.to_dict())

    def reset(self, user_key: str) -> WebmasterContext:
        ctx = WebmasterContext(key=user_key, stage="idle", task_kind="new_task")
        self.save(ctx)
        return ctx

    def load_prompt_learning(self) -> Dict[str, object]:
        payload: object = {"patches": [], "active_version": 1}
        try:
            with open(self.prompt_learning_path, "r", encoding="utf-8") as f:
                payload = yaml.safe_load(f)
        except FileNotFoundError:
            payload = {"patches": [], "active_version": 1}
        except yaml.YAMLError:
            _log.exception("webmaster prompt learning yaml parse failed: %s", self.prompt_learning_path)
            payload = {"patches": [], "active_version": 1}
        except Exception:
            _log.exception("webmaster prompt learning load failed: %s", self.prompt_learning_path)
            payload = {"patches": [], "active_version": 1}

        normalized = normalize_learning_payload(payload)
        try:
            if payload != normalized:
                self.save_prompt_learning(normalized)
        except Exception:
            _log.exception("webmaster prompt learning normalize-rewrite failed: %s", self.prompt_learning_path)
        return normalized

    def save_prompt_learning(self, data: Dict[str, object]) -> None:
        payload = normalize_learning_payload(data)
        with open(self.prompt_learning_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(payload, f, allow_unicode=True, sort_keys=False)
