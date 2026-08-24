"""Deterministic hard guards and soft warnings for the canonical story state."""

from __future__ import annotations

from typing import Any

from app.services.state_engine import get_story_state
from app.services.narrative_structure import narrative_location


def _plan_text(plan: dict[str, Any]) -> str:
    values = [str(plan.get(key) or "") for key in ("title", "goal", "conflict", "hook", "plot_arc", "opening_state", "ending_state")]
    for key in ("beats", "causal_beats", "forbidden_reveals", "state_changes", "knowledge_changes"):
        values.extend(str(value) for value in (plan.get(key) or []))
    return "\n".join(values)


def _flatten(value: Any) -> str:
    if isinstance(value, dict):
        return " ".join(_flatten(item) for item in value.values())
    if isinstance(value, list):
        return " ".join(_flatten(item) for item in value)
    return str(value or "")


def _death_state(value: Any) -> bool:
    text = _flatten(value).lower()
    return any(token in text for token in ("死亡", "已死", "身亡", "dead", "deceased"))


def validate_chapter_plan(work: dict[str, Any], plan: dict[str, Any], *, replacing_no: int | None = None) -> list[str]:
    """Return author-facing hard conflicts for a chapter contract."""
    errors: list[str] = []
    chapter_no = int(plan.get("chapter_no") or replacing_no or 0)
    mode = str(plan.get("time_mode") or "linear")
    if mode not in {"linear", "flashback", "parallel"}:
        errors.append("时间模式必须是 linear、flashback 或 parallel。")
    day = plan.get("story_day")
    if day is not None:
        try:
            day = int(day)
        except (TypeError, ValueError):
            errors.append("故事日必须是整数；可用负数表示锚点事件之前。")
            day = None
    phase_key = str(plan.get("phase_key") or "").strip()
    previous = [item for item in (work.get("chapter_plans") or []) if int(item.get("chapter_no") or 0) < chapter_no]
    if mode == "linear" and day is not None:
        known_days = [int(item["story_day"]) for item in previous if item.get("story_day") is not None and str(item.get("time_mode") or "linear") == "linear"]
        if known_days and day < max(known_days):
            errors.append("故事时间不能早于前一线性章节；倒叙必须显式标为 flashback。")
    if mode == "linear" and chapter_no > 1 and plan.get("previous_chapter_no") not in (None, chapter_no - 1):
        errors.append("线性章节必须承接上一章；非正常承接请使用 flashback 或 parallel。")

    phases = {item.get("phase_key"): item for item in (work.get("story_phases") or [])}
    if phase_key:
        phase = phases.get(phase_key)
        if not phase:
            errors.append(f"故事阶段“{phase_key}”不存在。")
        elif day is not None:
            start, end = phase.get("start_day"), phase.get("end_day")
            if (start is not None and day < int(start)) or (end is not None and day > int(end)):
                errors.append(f"第{chapter_no}章的故事日不在阶段“{phase.get('name') or phase_key}”范围内。")
            text = _plan_text(plan)
            for forbidden in phase.get("forbidden") or []:
                if str(forbidden).strip() and str(forbidden) in text:
                    errors.append(f"阶段“{phase.get('name') or phase_key}”禁止“{forbidden}”提前出现。")

    text = _plan_text(plan)
    volume, narrative_stage = narrative_location(work, chapter_no)
    if (work.get("story_volumes") or []) and not volume:
        errors.append(f"第{chapter_no}章不在任何已配置分卷范围内。")
    if (work.get("narrative_stages") or []) and not narrative_stage:
        errors.append(f"第{chapter_no}章不在任何叙事阶段范围内。")
    if volume and plan.get("volume_id") and str(plan.get("volume_id")) != str(volume.get("id")):
        errors.append(f"第{chapter_no}章归属的分卷与章节范围不一致。")
    if narrative_stage:
        if plan.get("narrative_stage_id") and str(plan.get("narrative_stage_id")) != str(narrative_stage.get("id")):
            errors.append(f"第{chapter_no}章归属的叙事阶段与章节范围不一致。")
        for forbidden in narrative_stage.get("forbidden_payoffs") or []:
            if str(forbidden).strip() and str(forbidden) in text:
                errors.append(f"叙事阶段“{narrative_stage.get('title')}”禁止“{forbidden}”提前出现。")
    if day is not None:
        for faction in work.get("factions") or []:
            name = str(faction.get("name") or "").strip()
            formed_day = faction.get("public_day")
            if formed_day is None:
                formed_day = faction.get("active_from_day")
            if formed_day is None:
                formed_day = faction.get("formed_day")
            if name and formed_day is not None and day < int(formed_day) and name in text:
                errors.append(f"势力“{name}”在故事日{formed_day}才成立或公开活动，不能作为第{chapter_no}章（故事日{day}）的现存行动者。")
            first_appearance = int(faction.get("first_appearance_chapter") or 0)
            if name and first_appearance and chapter_no < first_appearance and name in text:
                errors.append(f"势力“{name}”首次公开行动在第{first_appearance}章，当前章节不能直接使用其名义行动。")

    if work.get("id") and chapter_no > 0:
        state = get_story_state(work["id"], before_chapter=chapter_no)
        for character in work.get("characters") or []:
            character_id = str(character.get("id") or "")
            name = str(character.get("name") or "")
            confirmed = (state.get("characters") or {}).get(character_id, {})
            if name and name in text and _death_state(confirmed):
                errors.append(f"已死亡人物“{name}”不能参与当前场景。")
        for dependency in plan.get("dependencies") or []:
            if not isinstance(dependency, dict):
                continue
            if dependency.get("dependency_type") == "character_state":
                character_id = str(dependency.get("character_id") or dependency.get("dependency_key") or "")
                field = str(dependency.get("field") or "")
                expected = dependency.get("expected")
                actual = ((state.get("characters") or {}).get(character_id) or {}).get(field)
                if field and expected is not None and actual != expected:
                    errors.append(f"第{chapter_no}章依赖的角色状态“{field}”已变化，不能使用已撤销事实。")
    return list(dict.fromkeys(errors))


def chapter_plan_warnings(work: dict[str, Any], plan: dict[str, Any]) -> list[str]:
    """Non-blocking continuity cues shown before generation or save."""
    warnings: list[str] = []
    text = _plan_text(plan)
    if any(token in text for token in ("重伤", "骨折", "濒死")) and any(token in text for token in ("狂奔", "连续作战", "徒手翻越")):
        warnings.append("受伤程度与行动强度可能不一致。")
    if not plan.get("goal"):
        warnings.append("当前任务可能被忽略。")
    if not plan.get("causal_beats"):
        warnings.append("人物态度或行动变化缺少因果过渡。")
    return warnings


def validate_chapter_generation(work: dict[str, Any], chapter_no: int) -> list[str]:
    plan = next((item for item in (work.get("chapter_plans") or []) if int(item.get("chapter_no") or 0) == chapter_no), None)
    if not plan:
        return []
    errors = validate_chapter_plan(work, plan, replacing_no=chapter_no)
    if plan.get("stale_reason"):
        errors.append(f"本章大纲需要复核：{plan['stale_reason']}")
    return errors
