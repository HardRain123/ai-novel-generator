"""Builds a bounded, auditable context for each chapter generation."""

from typing import Any


def _chapter_text(item: dict[str, Any], limit: int = 1800) -> dict[str, Any]:
    content = str(item.get("content", ""))
    return {
        "chapter_no": item.get("chapter_no"),
        "title": item.get("title", ""),
        "excerpt": content[-limit:],
    }


def _state_snapshot(work: dict[str, Any], character_id: str, chapter_no: int) -> dict[str, Any]:
    """Replay only accepted facts from chapters before the one being generated."""
    state: dict[str, Any] = {}
    history = work.get("character_state_history") or []
    for item in history:
        if item.get("character_id") != character_id:
            continue
        if int(item.get("chapter_no") or 0) >= chapter_no:
            continue
        value = item.get("reviewed_value")
        if value is None:
            value = item.get("new_value")
        state[item.get("field", "")] = value
    return {key: value for key, value in state.items() if key}


def build_context(work: dict[str, Any], chapter_no: int, recent_limit: int = 2) -> dict[str, Any]:
    """只返回下一章所需的有限上下文，正式状态优先于未审核候选。"""
    plans = work.get("chapter_plans") or []
    plan = next((item for item in plans if item.get("chapter_no") == chapter_no), None)
    characters = work.get("characters") or []
    states = work.get("character_states") or []
    state_by_character = {item.get("character_id"): item for item in states}
    warnings: list[str] = []

    character_context = []
    for character in characters:
        state = state_by_character.get(character.get("id"))
        snapshot = _state_snapshot(work, character.get("id"), chapter_no)
        snapshot_chapter = max(
            [
                int(item.get("chapter_no") or 0)
                for item in (work.get("character_state_history") or [])
                if item.get("character_id") == character.get("id")
                and int(item.get("chapter_no") or 0) < chapter_no
            ],
            default=0,
        )
        latest_state_chapter = int((state or {}).get("as_of_chapter") or 0)
        state_is_stale = bool(state and state.get("source_extraction_status") == "superseded")
        if latest_state_chapter >= chapter_no and latest_state_chapter:
            warnings.append(
                f"人物“{character.get('name', '')}”存在第{latest_state_chapter}章之后的状态，已排除未来信息。"
            )
        if state_is_stale:
            warnings.append(f"人物“{character.get('name', '')}”的最新状态来自已重写章节，等待状态重建。")
        character_context.append({
            "id": character.get("id"),
            "name": character.get("name", ""),
            "role": character.get("role", ""),
            "goal": character.get("goal", ""),
            "conflict": character.get("conflict", ""),
            "personality": character.get("personality", ""),
            "knowledge": character.get("knowledge", ""),
            "confirmed_state": snapshot or ({"warning": "暂无可用的有效历史状态快照"} if latest_state_chapter >= chapter_no or state_is_stale else (state.get("state", {}) if state else {})),
            "state_as_of_chapter": snapshot_chapter if snapshot else (0 if latest_state_chapter >= chapter_no or state_is_stale else latest_state_chapter),
        })

    open_foreshadows = [
        item for item in (work.get("foreshadows") or [])
        if item.get("status", "open") == "open"
        and int(item.get("planted_chapter") or 0) <= chapter_no
        and (not item.get("expected_reveal_chapter") or item.get("expected_reveal_chapter") <= chapter_no + 3)
    ]
    confirmed_timeline = [
        item for item in (work.get("timeline_events") or [])
        if item.get("review_status", "confirmed") == "confirmed"
        and item.get("source_extraction_status", "applied") in {"applied", ""}
        and int(item.get("chapter_no") or 0) < chapter_no
    ][-20:]
    previous = [item for item in (work.get("chapters") or []) if int(item.get("chapter_no") or 0) < chapter_no][-recent_limit:]
    previous_plans = [item for item in plans if int(item.get("chapter_no") or 0) < chapter_no][-recent_limit:]
    next_plan = next((item for item in plans if int(item.get("chapter_no") or 0) == chapter_no + 1), None)

    return {
        "immutable_rules": work.get("story_bible") or {},
        "chapter_plan": plan or {"chapter_no": chapter_no, "title": f"第{chapter_no}章"},
        "previous_plans": previous_plans,
        "next_chapter_boundary": next_plan or {},
        "characters": character_context,
        "open_foreshadows": open_foreshadows,
        "confirmed_timeline": confirmed_timeline,
        "previous_chapters": [_chapter_text(item) for item in previous],
        "retrieval_policy": {
            "unconfirmed_state_extractions": "excluded",
            "unconfirmed_timeline_events": "excluded",
            "max_previous_chapters": recent_limit,
        },
        "continuity_warnings": warnings,
    }
