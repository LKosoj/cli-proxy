from __future__ import annotations

from hashlib import sha256
from typing import Any, Dict, Optional

from modes.sdk.runtime.openai_client import chat_completion


class WebmasterModeRunnerService:
    """Webmaster mode-owned runtime facade."""

    capabilities = frozenset({"webmaster_chat_completion", "webmaster_artifact_checkpoints"})

    def __init__(self, config: Any) -> None:
        self._config = config

    def set_config(self, config: Any) -> None:
        self._config = config

    async def chat_completion(
        self,
        config: Any,
        system: str,
        user: str,
        *,
        response_format: Optional[Dict[str, Any]] = None,
    ) -> str:
        effective_config = config if config is not None else self._config
        return str(
            await chat_completion(
                effective_config,
                str(system or ""),
                str(user or ""),
                response_format=response_format,
            )
            or ""
        )

    def build_checkpoint_payload(
        self,
        *,
        phase: str,
        status: str,
        iteration: int,
        stage: str,
        task_text: str = "",
        report_text: str = "",
        validation_status: str = "",
        gate_passed: Optional[bool] = None,
    ) -> Dict[str, Any]:
        loop_iteration = max(0, int(iteration or 0))
        payload: Dict[str, Any] = {
            "phase": str(phase or "").strip() or "dev",
            "unit_id": f"webmaster:{str(phase or 'dev').strip() or 'dev'}:{loop_iteration + 1}",
            "status": str(status or "").strip() or "started",
            "iteration": loop_iteration,
            "stage": str(stage or "").strip() or "idle",
        }
        task_preview = str(task_text or "").strip()
        if task_preview:
            payload["task_preview"] = task_preview[:500]
            payload["task_hash"] = f"sha256:{sha256(task_preview.encode('utf-8')).hexdigest()}"
        report_preview = str(report_text or "").strip()
        if report_preview:
            payload["report_preview"] = report_preview[:500]
        validation_token = str(validation_status or "").strip().upper()
        if validation_token:
            payload["validation_status"] = validation_token
        if gate_passed is not None:
            payload["gate_passed"] = bool(gate_passed)
        return payload

    def supports_capability(self, capability: str) -> bool:
        return str(capability or "").strip() in self.capabilities
