from __future__ import annotations

from typing import List

from .detectors import meaningful_files, score_pack
from .registry import PackRegistry
from .schema import PackDefinition, PackScore, PackSelection
from .synthesizer import synthesize_proposed_pack

_AUTO_SELECT_SCORE = 0.55
_AMBIGUOUS_GAP = 0.15


def select_packs(
    *,
    registry: PackRegistry,
    workdir: str,
    codebase_context: str = "",
    allow_proposed: bool = True,
) -> PackSelection:
    scores = [
        score_pack(pack, workdir=workdir, codebase_context=codebase_context)
        for pack in registry.all()
        if pack.pack_id != "core-baseline"
        and str(pack.lifecycle or "").strip() != "proposed"
        and str(pack.source or "").strip() != "proposed"
    ]
    scores.sort(key=lambda item: (-item.score, item.pack.pack_id))
    selected: List[PackScore] = []
    core = registry.get("core-baseline")
    if core is not None:
        selected.append(PackScore(pack=core, score=1.0, evidence=[], missing_groups=[]))

    eligible = [
        score for score in scores
        if score.score >= _pack_threshold(score.pack) and not score.missing_groups
    ]
    selected.extend(_compatible_selection(eligible))

    proposed: List[PackDefinition] = []
    status = "selected"
    reason = ""
    non_core_selected = [score for score in selected if score.pack.pack_id != "core-baseline"]
    if not non_core_selected:
        status = "proposed" if allow_proposed else "uncovered"
        reason = "no_builtin_pack_matched"
        if allow_proposed and meaningful_files(workdir):
            proposed.append(synthesize_proposed_pack(meaningful_paths=meaningful_files(workdir), reason=reason))

    if _has_ambiguous_conflict(eligible):
        status = "ambiguous"
        reason = "top_pack_scores_too_close"
        selected = [score for score in selected if score.pack.pack_id == "core-baseline"]

    return PackSelection(
        selected=selected,
        proposed=proposed,
        all_scores=scores,
        status=status,
        reason=reason,
    )


def _pack_threshold(pack: PackDefinition) -> float:
    try:
        return float((pack.detectors or {}).get("min_confidence") or _AUTO_SELECT_SCORE)
    except Exception:
        return _AUTO_SELECT_SCORE


def _compatible_selection(eligible: List[PackScore]) -> List[PackScore]:
    selected: List[PackScore] = []
    selected_ids: set[str] = set()
    for score in eligible:
        pack_id = score.pack.pack_id
        if pack_id in selected_ids:
            continue
        applies = score.pack.applies_to or {}
        can_combine = {str(x) for x in list(applies.get("can_combine_with") or [])}
        primary = str(applies.get("primary_ecosystem") or "").strip()
        selected_primary = [
            str(item.pack.applies_to.get("primary_ecosystem") or "").strip()
            for item in selected
            if str(item.pack.applies_to.get("primary_ecosystem") or "").strip()
        ]
        if primary and selected_primary and primary not in selected_primary and not can_combine.intersection(selected_ids):
            # Keep secondary/cross-cutting packs combinable; avoid silently selecting conflicting primary stacks.
            if "any" not in can_combine:
                continue
        selected.append(score)
        selected_ids.add(pack_id)
    return selected


def _packs_conflict(left: PackDefinition, right: PackDefinition) -> bool:
    left_applies = left.applies_to or {}
    right_applies = right.applies_to or {}
    left_primary = str(left_applies.get("primary_ecosystem") or "").strip()
    right_primary = str(right_applies.get("primary_ecosystem") or "").strip()
    if not left_primary or not right_primary or left_primary == right_primary:
        return False
    left_can = {str(x) for x in list(left_applies.get("can_combine_with") or [])}
    right_can = {str(x) for x in list(right_applies.get("can_combine_with") or [])}
    if "any" in left_can or "any" in right_can:
        return False
    return right.pack_id not in left_can and left.pack_id not in right_can


def _has_ambiguous_conflict(eligible: List[PackScore]) -> bool:
    for idx, left in enumerate(eligible):
        for right in eligible[idx + 1:]:
            if abs(float(left.score) - float(right.score)) >= _AMBIGUOUS_GAP:
                continue
            if _packs_conflict(left.pack, right.pack):
                return True
    return False
