"""Range-aware, batched chapter-outline planning for V2."""

from __future__ import annotations

from copy import deepcopy
from typing import Any
from uuid import uuid4

from app.db import transaction
from app.services.novel_engine import engine
from app.services.narrative_structure import DEFAULT_DETAIL_WINDOW, narrative_location, narrative_window, suggested_target_chapters
from app.services.planning_quality import evaluate_outline
from app.services.state_engine import get_story_state
from app.services.story_state import validate_chapter_plan
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


def _chapter_issues(
    work: dict[str, Any],
    item: dict[str, Any],
    *,
    raw_phase_missing: bool = False,
) -> list[str]:
    """Validate one chapter before any batch-level aggregation occurs."""
    chapter_no = int(item.get("chapter_no") or 0)
    issues, _score = evaluate_outline(work, [item], 1, expected_from_chapter=chapter_no)
    if raw_phase_missing:
        issues.append(f"第{chapter_no}章缺少模型返回的故事阶段标识。")
    known_mainlines = {
        str(candidate.get("title") or "").strip()
        for candidate in [*(work.get("plot_arcs") or []), *(work.get("story_volumes") or [])]
        if str(candidate.get("title") or "").strip()
    }
    plot_arc = str(item.get("plot_arc") or "").strip()
    if known_mainlines and plot_arc and plot_arc not in known_mainlines:
        issues.append(f"第{chapter_no}章所属主线“{plot_arc}”不存在于已确认卷级主线。")
    issues.extend(validate_chapter_plan(work, item, replacing_no=chapter_no))
    return list(dict.fromkeys(issues))


def _batch_structure_issues(items: list[dict[str, Any]], expected_numbers: list[int]) -> list[str]:
    numbers = [int(item.get("chapter_no") or 0) for item in items]
    issues: list[str] = []
    if len(items) != len(expected_numbers):
        issues.append(f"要求 {len(expected_numbers)} 章，实际返回 {len(items)} 章。")
    if numbers != expected_numbers:
        issues.append("整批章节编号不连续或顺序错误。")
    return issues


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
    repair_history: list[dict[str, Any]] = []
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
        expected_numbers = list(range(batch_start, batch_end + 1))
        items = raw.get("chapters") or []
        structure_issues = _batch_structure_issues(items, expected_numbers)
        if structure_issues:
            original_items = deepcopy(items)
            repair_data = engine.repair_outline_chapters(
                current_work,
                [{"chapter_no": chapter_no, "chapter": next((item for item in items if int(item.get("chapter_no") or 0) == chapter_no), {})} for chapter_no in expected_numbers],
                {"batch": structure_issues},
                profile,
                generation_context={**generation_context, "target_chapter_numbers": expected_numbers},
            )
            repaired_items = repair_data.get("chapters") if isinstance(repair_data, dict) else []
            repair_history.append({
                "scope": "batch",
                "chapter_numbers": expected_numbers,
                "original_output": original_items,
                "issues": {"batch": structure_issues},
                "repair_result": deepcopy(repair_data),
            })
            if isinstance(repaired_items, list):
                items = repaired_items
            structure_issues = _batch_structure_issues(items, expected_numbers)
            if structure_issues:
                raise ValueError("分批大纲结构修复后仍不完整：" + "；".join(structure_issues))
        batch: list[dict[str, Any]] = []
        original_raw_by_number = {
            int(item.get("chapter_no")): deepcopy(item)
            for item in items
            if isinstance(item, dict) and str(item.get("chapter_no") or "").lstrip("-").isdigit()
        }
        chapter_issues: dict[int, list[str]] = {}
        for index, item in enumerate(items):
            item_work = {**current_work, "chapter_plans": [*(current_work.get("chapter_plans") or []), *batch]}
            chapter_no = batch_start + index
            raw_phase_missing = not str(item.get("phase_key") or "").strip()
            decorated = _decorate(item, chapter_no, item_work, fact_version)
            issues = _chapter_issues(item_work, decorated, raw_phase_missing=raw_phase_missing)
            if issues:
                chapter_issues[chapter_no] = issues
            batch.append(decorated)
        if chapter_issues:
            failed_numbers = sorted(chapter_issues)
            original_items = [
                deepcopy(original_raw_by_number.get(chapter_no, next(item for item in batch if item.get("chapter_no") == chapter_no)))
                for chapter_no in failed_numbers
            ]
            repair_data = engine.repair_outline_chapters(
                current_work,
                [{"chapter_no": chapter_no, "chapter": next(item for item in batch if item.get("chapter_no") == chapter_no)} for chapter_no in failed_numbers],
                {str(chapter_no): issues for chapter_no, issues in chapter_issues.items()},
                profile,
                generation_context={**generation_context, "target_chapter_numbers": failed_numbers},
            )
            repaired_items = repair_data.get("chapters") if isinstance(repair_data, dict) else []
            repaired_by_number = {
                int(item.get("chapter_no")): item
                for item in repaired_items or []
                if isinstance(item, dict) and str(item.get("chapter_no") or "").lstrip("-").isdigit()
            }
            for index, item in enumerate(batch):
                chapter_no = int(item.get("chapter_no") or 0)
                if chapter_no in repaired_by_number:
                    batch[index] = _decorate(repaired_by_number[chapter_no], chapter_no, current_work, fact_version)
            repair_history.append({
                "scope": "chapters",
                "chapter_numbers": failed_numbers,
                "original_output": original_items,
                "issues": {str(chapter_no): issues for chapter_no, issues in chapter_issues.items()},
                "repair_result": deepcopy(repair_data),
            })
            remaining_issues: dict[int, list[str]] = {}
            recheck_work = {**current_work, "chapter_plans": [*(current_work.get("chapter_plans") or []), *batch]}
            for item in batch:
                chapter_no = int(item.get("chapter_no") or 0)
                issues = _chapter_issues(recheck_work, item)
                if issues:
                    remaining_issues[chapter_no] = issues
            if remaining_issues:
                raise ValueError(
                    "章节定向修复后仍有问题："
                    + "；".join(f"第{chapter_no}章：{'、'.join(issues)}" for chapter_no, issues in remaining_issues.items())
                )
            quality_issues.extend(issue for issues in chapter_issues.values() for issue in issues)
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
        "repair_history": repair_history,
        "repair_count": len(repair_history),
        "batch_size": BATCH_SIZE,
        "batches": [
            {"from_chapter": start, "to_chapter": min(start + BATCH_SIZE - 1, to_chapter)}
            for start in range(from_chapter, to_chapter + 1, BATCH_SIZE)
        ],
    }
