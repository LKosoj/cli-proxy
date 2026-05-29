from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, MutableMapping, Optional


RunModePipelineFn = Callable[[Any, str, dict, Any, str], Awaitable[None]]


@dataclass
class ModePipelineService:
    run_mode_pipeline_fn: Optional[RunModePipelineFn] = None

    async def run_mode_pipeline(self, session: Any, prompt: str, dest: dict, context: Any, *, mode_id: str) -> None:
        if not self.run_mode_pipeline_fn:
            raise RuntimeError("ModePipelineService.run_mode_pipeline_fn is not configured")
        await self.run_mode_pipeline_fn(session, str(prompt), dict(dest or {}), context, str(mode_id))


InterruptSessionFn = Callable[[str, int, Any], None]
ClearSandboxFn = Callable[[Optional[int]], tuple[int, int]]
ClearSessionFilesFn = Callable[[str], bool]
ClearSessionCacheFn = Callable[[str], None]
GetSessionFn = Callable[[int, str], Any]
GetSessionByUidFn = Callable[[str, Optional[int]], Any]
logger = logging.getLogger(__name__)


@dataclass
class AgentRuntimeService:
    interrupt_session_fn: Optional[InterruptSessionFn] = None
    clear_sandbox_fn: Optional[ClearSandboxFn] = None
    clear_session_files_fn: Optional[ClearSessionFilesFn] = None
    clear_session_cache_fn: Optional[ClearSessionCacheFn] = None
    get_session_fn: Optional[GetSessionFn] = None
    get_session_by_uid_fn: Optional[GetSessionByUidFn] = None

    def interrupt_session(self, session_id: str, chat_id: int, context: Any) -> None:
        if not self.interrupt_session_fn:
            raise RuntimeError("AgentRuntimeService.interrupt_session_fn is not configured")
        self.interrupt_session_fn(str(session_id), chat_id, context)

    def clear_sandbox(self, *, chat_id: Optional[int] = None) -> tuple[int, int]:
        if not self.clear_sandbox_fn:
            raise RuntimeError("AgentRuntimeService.clear_sandbox_fn is not configured")
        return tuple(self.clear_sandbox_fn(chat_id))

    def clear_session_files(self, session_id: str) -> bool:
        if not self.clear_session_files_fn:
            raise RuntimeError("AgentRuntimeService.clear_session_files_fn is not configured")
        return bool(self.clear_session_files_fn(str(session_id)))

    def clear_session_cache(self, session_id: str) -> None:
        if not self.clear_session_cache_fn:
            raise RuntimeError("AgentRuntimeService.clear_session_cache_fn is not configured")
        self.clear_session_cache_fn(str(session_id))

    def get_session(self, chat_id: int, session_id: str) -> Any:
        if not self.get_session_fn:
            raise RuntimeError("AgentRuntimeService.get_session_fn is not configured")
        return self.get_session_fn(chat_id, str(session_id))

    def get_session_by_uid(self, session_uid: str, *, chat_id: Optional[int] = None) -> Any:
        if not self.get_session_by_uid_fn:
            logger.warning("agent runtime session lookup backend unavailable session_uid=%s", session_uid)
            return None
        try:
            return self.get_session_by_uid_fn(str(session_uid or ""), chat_id)
        except Exception:
            logger.exception(
                "agent runtime session lookup failed session_uid=%s chat_id=%s",
                session_uid,
                chat_id,
            )
            return None


StartDirsFlowFn = Callable[[Any, Any, str, str], Awaitable[None]]
ClearDirsFlowFn = Callable[[Any, str, str], None]
GetDirsModeTokenFn = Callable[[Any, Optional[int]], str]


@dataclass
class DirsFlowService:
    start_flow_fn: Optional[StartDirsFlowFn] = None
    clear_flow_fn: Optional[ClearDirsFlowFn] = None
    get_mode_token_fn: Optional[GetDirsModeTokenFn] = None

    async def start_flow(self, chat_id: Any, context: Any, *, root: str, mode_token: str) -> None:
        if not self.start_flow_fn:
            raise RuntimeError("DirsFlowService.start_flow_fn is not configured")
        await self.start_flow_fn(chat_id, context, str(root), str(mode_token))

    def clear_flow(self, chat_id: Any, *, mode_id: str = "", flow: str = "") -> None:
        if not self.clear_flow_fn:
            return
        self.clear_flow_fn(chat_id, str(mode_id or ""), str(flow or ""))

    def active_token(self, chat_id: Any, *, message_thread_id: Optional[int] = None) -> str:
        if not self.get_mode_token_fn:
            logger.warning(
                "dirs flow token backend unavailable chat_id=%s message_thread_id=%s",
                chat_id,
                message_thread_id,
            )
            return ""
        try:
            return str(self.get_mode_token_fn(chat_id, message_thread_id) or "")
        except Exception:
            logger.exception(
                "dirs flow token lookup failed chat_id=%s message_thread_id=%s",
                chat_id,
                message_thread_id,
            )
            return ""


@dataclass
class DictStateService:
    store: MutableMapping[str, Any]

    def get(self, key: str, default: Any = None) -> Any:
        return self.store.get(str(key), default)

    def set(self, key: str, value: Any) -> None:
        self.store[str(key)] = value

    def pop(self, key: str, default: Any = None) -> Any:
        return self.store.pop(str(key), default)

    def delete(self, key: str) -> bool:
        skey = str(key)
        if skey in self.store:
            self.store.pop(skey, None)
            return True
        return False
