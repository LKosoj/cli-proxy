from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List


@dataclass
class WebmasterContext:
    key: str
    task_kind: str = "new_task"
    stage: str = "idle"
    goal: str = ""
    actions: List[str] = field(default_factory=list)
    constraints: List[str] = field(default_factory=list)
    acceptance_criteria: List[str] = field(default_factory=list)
    ambiguities: List[str] = field(default_factory=list)
    assumptions: List[str] = field(default_factory=list)
    last_cli_task: str = ""
    last_cli_report: str = ""
    last_feedback_class: str = ""
    last_user_text: str = ""
    last_validation_report: str = ""
    last_validation_json: Dict[str, object] = field(default_factory=dict)
    last_fix_pack: List[Dict[str, str]] = field(default_factory=list)
    prompt_patches: List[Dict[str, object]] = field(default_factory=list)
    updated_at: float = 0.0
    active_prompt_version: int = 1
    confirmation_attempts: int = 0
    fix_iteration_count: int = 0
    metadata: Dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Dict[str, object], key: str) -> "WebmasterContext":
        return cls(
            key=key,
            task_kind=str(data.get("task_kind", "new_task") or "new_task"),
            stage=str(data.get("stage", "idle") or "idle"),
            goal=str(data.get("goal", "") or ""),
            actions=[str(x).strip() for x in (data.get("actions") or []) if str(x).strip()],
            constraints=[str(x).strip() for x in (data.get("constraints") or []) if str(x).strip()],
            acceptance_criteria=[str(x).strip() for x in (data.get("acceptance_criteria") or []) if str(x).strip()],
            ambiguities=[str(x).strip() for x in (data.get("ambiguities") or []) if str(x).strip()],
            assumptions=[str(x).strip() for x in (data.get("assumptions") or []) if str(x).strip()],
            last_cli_task=str(data.get("last_cli_task", "") or ""),
            last_cli_report=str(data.get("last_cli_report", "") or ""),
            last_feedback_class=str(data.get("last_feedback_class", "") or ""),
            last_user_text=str(data.get("last_user_text", "") or ""),
            last_validation_report=str(data.get("last_validation_report", "") or ""),
            last_validation_json=(
                dict(data.get("last_validation_json") or {})
                if isinstance(data.get("last_validation_json"), dict)
                else {}
            ),
            last_fix_pack=[
                {
                    "severity": str(x.get("severity") or "").strip(),
                    "title": str(x.get("title") or "").strip(),
                    "location": str(x.get("location") or "").strip(),
                    "why": str(x.get("why") or "").strip(),
                    "fix_hint": str(x.get("fix_hint") or "").strip(),
                }
                for x in (data.get("last_fix_pack") or [])
                if isinstance(x, dict)
            ],
            prompt_patches=[x for x in (data.get("prompt_patches") or []) if isinstance(x, dict)],
            updated_at=float(data.get("updated_at", 0.0) or 0.0),
            active_prompt_version=int(data.get("active_prompt_version", 1) or 1),
            confirmation_attempts=int(data.get("confirmation_attempts", 0) or 0),
            fix_iteration_count=int(data.get("fix_iteration_count", 0) or 0),
            metadata={str(k): str(v) for k, v in (data.get("metadata") or {}).items()} if isinstance(data.get("metadata"), dict) else {},
        )

    def to_dict(self) -> Dict[str, object]:
        return {
            "task_kind": self.task_kind,
            "stage": self.stage,
            "goal": self.goal,
            "actions": list(self.actions),
            "constraints": list(self.constraints),
            "acceptance_criteria": list(self.acceptance_criteria),
            "ambiguities": list(self.ambiguities),
            "assumptions": list(self.assumptions),
            "last_cli_task": self.last_cli_task,
            "last_cli_report": self.last_cli_report,
            "last_feedback_class": self.last_feedback_class,
            "last_user_text": self.last_user_text,
            "last_validation_report": self.last_validation_report,
            "last_validation_json": dict(self.last_validation_json),
            "last_fix_pack": list(self.last_fix_pack),
            "prompt_patches": list(self.prompt_patches),
            "updated_at": self.updated_at,
            "active_prompt_version": self.active_prompt_version,
            "confirmation_attempts": self.confirmation_attempts,
            "fix_iteration_count": self.fix_iteration_count,
            "metadata": dict(self.metadata),
        }


@dataclass
class FeedbackDecision:
    kind: str
    reason: str


@dataclass
class ValidationDecision:
    status: str
    summary: str
    blocking_issues: List[str]
    checklist_rows: List[Dict[str, str]]
    defects: List[Dict[str, str]]
    raw: Dict[str, object]
