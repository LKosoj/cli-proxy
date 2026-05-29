import os
import shutil

from sessions.scoped_key import is_session_scoped_key, sanitize_scoped_key_token
from utils.paths import sandbox_root, sandbox_session_dir, sandbox_shared_dir


class AgentSandboxService:
    def __init__(self, workdir: str):
        self.workdir = str(workdir)

    def configure(self) -> None:
        root = self.root()
        shared = sandbox_shared_dir(self.workdir)
        chats = os.path.join(shared, "chats")
        chat_workspaces = os.path.join(root, "chats")
        sessions = os.path.join(root, "sessions")
        os.makedirs(root, exist_ok=True)
        os.makedirs(shared, exist_ok=True)
        os.makedirs(chats, exist_ok=True)
        os.makedirs(chat_workspaces, exist_ok=True)
        os.makedirs(sessions, exist_ok=True)
        os.environ["AGENT_SANDBOX_ROOT"] = root

    def root(self) -> str:
        return sandbox_root(self.workdir)

    def service_entries(self) -> set[str]:
        return {"_shared"}

    def chat_workspace(self, chat_id: int | str | None) -> str:
        cid = str(chat_id or 0)
        return os.path.join(self.root(), "chats", f"chat_{cid}" if isinstance(cid, int) or cid.isdigit() else f"chat_str_{cid}")

    def clear(self, chat_id: int | str | None = None) -> tuple[int, int]:
        root = self.root()
        if not os.path.isdir(root):
            return 0, 0
        if chat_id is not None:
            return self._clear_chat(chat_id)
        removed = 0
        errors = 0
        for name in os.listdir(root):
            if name in self.service_entries():
                continue
            path = os.path.join(root, name)
            try:
                if os.path.isdir(path) and not os.path.islink(path):
                    shutil.rmtree(path)
                else:
                    os.remove(path)
                removed += 1
            except Exception:
                errors += 1
        return removed, errors

    def _clear_chat(self, chat_id: int | str) -> tuple[int, int]:
        removed = 0
        errors = 0
        target = self.chat_workspace(chat_id)
        try:
            if os.path.isdir(target) and not os.path.islink(target):
                shutil.rmtree(target)
                removed += 1
            elif os.path.exists(target):
                os.remove(target)
                removed += 1
        except Exception:
            errors += 1

        # Chat-specific shared history/log under _sandbox/_shared/chats/chat_<id>.md
        chat_file = os.path.join(sandbox_shared_dir(self.workdir), "chats", f"chat_{chat_id}.md")
        try:
            if os.path.isfile(chat_file):
                os.remove(chat_file)
                removed += 1
        except Exception:
            errors += 1
        return removed, errors

    def clear_session(self, session_scoped_key: str) -> bool:
        root = self.root()
        token = sanitize_scoped_key_token(session_scoped_key)
        if not is_session_scoped_key(token):
            return False
        session_dir = sandbox_session_dir(self.workdir, token)
        try:
            real_root = os.path.realpath(root)
            real_target = os.path.realpath(session_dir)
            if not real_target.startswith(real_root + os.sep):
                return False
            if os.path.isdir(real_target):
                shutil.rmtree(real_target)
                return True
            if os.path.exists(real_target):
                os.remove(real_target)
                return True
            return True
        except Exception:
            return False
