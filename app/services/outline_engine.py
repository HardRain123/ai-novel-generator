"""Range-aware, batched chapter-outline planning for V2."""

from __future__ import annotations

from copy import deepcopy
from typing import Any
from uuid import uuid4

from app.db import transaction
from app.services.novel_engine import engine
from app.services.narrative_structure import DEFAULT_DETAIL_WINDOW, narrative_location, narrative_window, suggested_target_chapters
from app.services.state_engine import get_story_state
from app.utils import json_dumps, now_iso


BATCH_SIZE = 12


def ensure_lifecycle_candidates(work_id: str) -> None:
    """Give a new work an explicit (but unlocked) baseline phase.

    It is a reviewable candidate, never a hidden genre-specific rule.
    """
    with transaction() as conn:
        exists = conn.execute("SELECT 1 FROM story_phases WHERE work_id=? LIMIT 1", (work_id,)).fetchone()
        if exists:
            return
        now = now_iso()
        conn.execute(
            """INSERT INTO story_phases(
                id,work_id,phase_key,name,start_day,end_day,rules_json,locked,created_at,updated_at,
                allowed_json,forbidden_json,transition_conditions_json
            ) VALUES (?,?, 'default','待确认默认阶段',NULL,NULL,'[]',0,?,?, '[]','[]','[]')""",
            (str(uuid4()), work_id, now, now),
        )


def resolve_outline_range(work: dict[str, Any], request: dict[str, Any]) -> tuple[int, int, str, int]:
    mode = str(request.get("mode") or "initial")
    legacy_count = request.get("chapter_count")
    total_target = int(request.get("total_target_chapters") or legacy_count or work.get("target_chapter_count") or suggested_target_chapters(work.get("estimated_words"), work.get("average_chapter_words")))
    if not 1 <= total_target <= 10000:
        raise ValueError("全书目标章节数必须在 1 到 10000 之间")
    requested_to = request.get("to_chapter")
    to_chapter = int(requested_to) if requested_to is not None else None
    requested_from = int(request.get("from_chapter") or 1)
    existing_last = max((int(item.get("chapter_no") or 0) for item in work.get("chapter_plans") or []), default=0)
    if mode == "initial":
        if requested_from != 1:
            raise ValueError("首次生成必须从第1章开始")
        # Legacy requests treated chapter_count as both total and end chapter.
        to_chapter = to_chapter or (int(legacy_count) if legacy_count else min(total_target, DEFAULT_DETAIL_WINDOW))
        if to_chapter > total_target:
            raise ValueError("本次生成终点不能超过全书目标章节数")
        return 1, to_chapter, mode, total_target
    if mode == "replan":
        to_chapter = to_chapter or min(total_target, requested_from + DEFAULT_DETAIL_WINDOW - 1)
        if requested_from > to_chapter:
            raise ValueError("重新规划的起始章节不能晚于结束章节")
        if to_chapter > total_target:
            raise ValueError("重新规划终点不能超过全书目标章节数")
        return requested_from, to_chapter, mode, total_target
    if mode == "extend":
        if total_target <= existing_last:
            raise ValueError("全书目标章节数必须大于当前已规划章节数")
        if request.get("from_chapter") not in (None, 1, existing_last + 1):
            raise ValueError("扩展从现有最后一章之后开始，不能覆盖已有大纲")
        to_chapter = to_chapter or min(total_target, existing_last + DEFAULT_DETAIL_WINDOW)
        if to_chapter <= existing_last or to_chapter > total_target:
            raise ValueError("扩展范围必须位于已规划章节之后且不超过全书目标")
        return existing_last + 1, to_chapter, mode, total_target
    raise ValueError("不支持的大纲模式")


def _phase_for_day(work: dict[str, Any], story_day: int) -> str:
    matches: list[dict[str, Any]] = []
    for phase in work.get("story_phases") or []:
        start, end = phase.get("start_day"), phase.get("end_day")
        if (start is None or story_day >= int(start)) and (end is None or story_day <= int(end)):
            matches.append(phase)
    if matches:
        # The unbounded default phase is a safety net.  A dated author-defined
        # phase must take precedence when both match the same story day.
        matches.sort(key=lambda phase: (phase.get("start_day") is None and phase.get("end_day") is None, str(phase.get("phase_key") or "")))
        return str(matches[0].get("phase_key") or "default")
    return "default"


def _canonical_phase_key(work: dict[str, Any], supplied_key: Any, story_day: int) -> str:
    """Turn model-facing phase labels into persisted phase keys.

    Models naturally return a narrative label such as "建立".  The database,
    however, stores a stable machine key (often just ``default``).  Preserve a
    real key, accept an unambiguous display-name match, and otherwise derive
    the phase from the chapter's story day instead of failing the whole job.
    """
    supplied = str(supplied_key or "").strip()
    phases = work.get("story_phases") or []
    keys = {str(phase.get("phase_key") or "").strip() for phase in phases}
    if supplied and supplied in keys:
        return supplied
    named = [
        str(phase.get("phase_key") or "").strip()
        for phase in phases
        if supplied and supplied == str(phase.get("name") or "").strip()
    ]
    if len(named) == 1 and named[0]:
        return named[0]
    return _phase_for_day(work, story_day)


def _decorate(item: dict[str, Any], chapter_no: int, work: dict[str, Any], fact_version: int) -> dict[str, Any]:
    result = deepcopy(item)
    previous = next((plan for plan in work.get("chapter_plans") or [] if int(plan.get("chapter_no") or 0) == chapter_no - 1), None)
    previous_day = previous.get("story_day") if previous else None
    story_day = result.get("story_day")
    if story_day is None:
        story_day = int(previous_day) + 1 if previous_day is not None else chapter_no - 1
    elif previous_day is not None and str(result.get("time_mode") or "linear") == "linear" and int(story_day) <= int(previous_day):
        # Each batch is generated with a compact local numbering prompt.  Its
        # relative fallback day must be rebased onto the preceding batch.
        story_day = int(previous_day) + 1
    previous_chapter_no = result.get("previous_chapter_no")
    if str(result.get("time_mode") or "linear") == "linear":
        previous_chapter_no = chapter_no - 1 if chapter_no > 1 else None
    elif previous_chapter_no is None and chapter_no > 1:
        previous_chapter_no = chapter_no - 1
    result.update({
        "chapter_no": chapter_no,
        "story_day": int(story_day),
        "phase_key": _canonical_phase_key(work, result.get("phase_key"), int(story_day)),
        "time_mode": str(result.get("time_mode") or "linear"),
        "start_time": str(result.get("start_time") or ""),
        "end_time": str(result.get("end_time") or ""),
        "previous_chapter_no": previous_chapter_no,
        "fact_version": fact_version,
        "calibration_status": "calibrated",
    })
    volume, stage = narrative_location(work, chapter_no)
    if volume:
        result["volume_id"] = volume.get("id")
        result["plot_arc"] = volume.get("title") or result.get("plot_arc", "")
    if stage:
        result["narrative_stage_id"] = stage.get("id")
    return result


def generate_outline_batches(
    work: dict[str, Any], request: dict[str, Any], profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Generate detailed outlines in fixed batches while carrying formal context forward."""
    from_chapter, to_chapter, mode, total_target_chapters = resolve_outline_range(work, request)
    fact_version = int(work.get("fact_version") or 0)
    state = get_story_state(work["id"], before_chapter=from_chapter) if mode == "replan" else get_story_state(work["id"], at_chapter=0)
    generated: list[dict[str, Any]] = []
    source = "fallback"
    quality_issues: list[str] = []
    current_work = deepcopy(work)
    current_work["outline_state_context"] = state
    current_work["future_planning_context"] = current_work.get("future_plans") or []
    for batch_start in range(from_chapter, to_chapter + 1, BATCH_SIZE):
        batch_end = min(batch_start + BATCH_SIZE - 1, to_chapter)
        generation_context = {
            "from_chapter": batch_start,
            "to_chapter": batch_end,
            "total_target_chapters": total_target_chapters,
            **narrative_window(current_work, batch_start, batch_end),
        }
        raw = engine.generate_outline(current_work, batch_end - batch_start + 1, profile, generation_context=generation_context)
        source = "model" if raw.get("generation_source") == "model" else source
        quality_issues.extend(raw.get("quality_issues") or [])
        items = raw.get("chapters") or []
        if len(items) != batch_end - batch_start + 1:
            raise ValueError("分批大纲生成返回的章节数与请求范围不一致")
        batch: list[dict[str, Any]] = []
        for index, item in enumerate(items):
            item_work = {**current_work, "chapter_plans": [*(current_work.get("chapter_plans") or []), *batch]}
            batch.append(_decorate(item, batch_start + index, item_work, fact_version))
        generated.extend(batch)
        current_work["chapter_plans"] = [
            *(current_work.get("chapter_plans") or []), *batch,
        ]
    return {
        "chapters": generated,
        "mode": mode,
        "from_chapter": from_chapter,
        "to_chapter": to_chapter,
        "target_chapter_count": total_target_chapters,
        "total_target_chapters": total_target_chapters,
        "fact_version": fact_version,
        "generation_source": source,
        "quality_issues": list(dict.fromkeys(quality_issues)),
        "batch_size": BATCH_SIZE,
        "batches": [
            {"from_chapter": start, "to_chapter": min(start + BATCH_SIZE - 1, to_chapter)}
            for start in range(from_chapter, to_chapter + 1, BATCH_SIZE)
        ],
    }
