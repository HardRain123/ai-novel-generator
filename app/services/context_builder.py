"""Build bounded, reproducible and auditable model contexts."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from app.db import transaction
from app.services.character_cards import character_context
from app.services.state_engine import get_story_state
from app.utils import json_dumps, json_loads, now_iso


def _chapter_text(item: dict[str, Any], limit: int = 1800) -> dict[str, Any]:
    content = str(item.get("content", ""))
    return {"chapter_no": item.get("chapter_no"), "title": item.get("title", ""), "excerpt": content[-limit:]}


def _is_active_faction(faction: dict[str, Any], day: int | None) -> bool:
    if day is None:
        return False
    formed = faction.get("formed_day")
    active_from = faction.get("active_from_day")
    dissolved = faction.get("dissolved_day")
    start = active_from if active_from is not None else formed
    return start is not None and day >= int(start) and (dissolved is None or day < int(dissolved))


def _long_term_character(character: dict[str, Any]) -> dict[str, Any]:
    """Project the stable compact card; chapter state is supplied separately."""
    return character_context(character)


def _non_empty_fields(item: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    """Keep prompt payloads focused without accidentally serializing empty schema fields."""
    return {
        field: item[field]
        for field in fields
        if item.get(field) not in (None, "", [], {}, ())
    }


def _planned_names(plan: dict[str, Any]) -> set[str]:
    names = {str(plan.get("pov_character") or "").strip()}
    appearing = plan.get("appearing_characters") or []
    if isinstance(appearing, str):
        appearing = [appearing]
    names.update(str(name or "").strip() for name in appearing)
    return {name for name in names if name}


def _chapter_contract(plan: dict[str, Any]) -> dict[str, Any]:
    """Return the executable part of a plan, rather than its persistence metadata."""
    return _non_empty_fields(plan, (
        "chapter_no", "title", "pov_character", "goal", "conflict", "failure_cost",
        "opening_state", "beats", "causal_beats", "knowledge_changes", "state_changes",
        "foreshadow_actions", "forbidden_reveals", "ending_state", "hook",
        "appearing_characters", "appearing_factions", "character_arc_progress",
    ))


def _next_boundary(next_plan: dict[str, Any] | None) -> dict[str, Any]:
    """Expose only the next chapter's boundary, never its full scene checklist."""
    if not next_plan:
        return {}
    return _non_empty_fields(next_plan, ("chapter_no", "title", "goal", "conflict", "opening_state"))


def _chapter_character(character: dict[str, Any]) -> dict[str, Any]:
    """Keep only material that can influence a present-scene emotional choice."""
    core = character.get("dramatic_core") or {}
    state = character.get("confirmed_state") or {}
    result = _non_empty_fields(character, (
        "id", "name", "role", "biography", "personality", "voice", "relationships",
        "arc", "secret", "knowledge_scope", "state_as_of_chapter",
    ))
    if isinstance(core, dict):
        result["dramatic_core"] = _non_empty_fields(core, ("goal", "motivation", "flaw", "conflict"))
    if state:
        result["confirmed_state"] = state
    return result


def chapter_generation_context(base_context: dict[str, Any]) -> dict[str, Any]:
    """Project a factual context into the smaller payload used by the chapter writer.

    The full context remains available for state tooling.  The writer only needs
    material that can change this chapter's choices, voice, and immediate
    continuity; passing every future plan or inactive cast card makes prose read
    like a checklist.
    """
    plan = dict(base_context.get("chapter_plan") or {})
    names = _planned_names(plan)
    appearing_factions = plan.get("appearing_factions") or []
    if isinstance(appearing_factions, str):
        appearing_factions = [appearing_factions]
    faction_names = {str(name or "").strip() for name in appearing_factions if str(name or "").strip()}
    cast = [
        _chapter_character(item)
        for item in (base_context.get("characters") or [])
        if not names or str(item.get("name") or "") in names
    ]
    if names:
        cast = [item for item in cast if item.get("name")]

    bible = dict(base_context.get("long_term_rules") or {})
    previous_chapters = [
        {
            **_non_empty_fields(item, ("chapter_no", "title")),
            "excerpt": str(item.get("excerpt") or "")[-900:],
        }
        for item in (base_context.get("previous_chapters") or [])
    ]
    previous_chapters = [item for item in previous_chapters if item.get("excerpt")]

    return {
        "chapter_contract": _chapter_contract(plan),
        "story_rules": _non_empty_fields(bible, (
            "summary", "world", "style_rules", "reader_promise", "core_hook",
            "core_conflict", "stakes", "must_have_elements", "avoid_drift",
        )),
        "long_term_facts": [
            _non_empty_fields(item, ("entity_type", "entity_id", "fact_key", "value", "locked"))
            for item in (base_context.get("long_term_facts") or [])[:20]
        ],
        "relevant_characters": cast,
        "emotional_direction": {
            "pov_arc_step": str(plan.get("character_arc_progress") or "").strip(),
            "pressure_or_cost": str(plan.get("failure_cost") or "").strip(),
            "instruction": "关键转折要写出触发、人物的具体主观反应，以及该反应如何改变选择、动作或对白；不得凭空补造创伤或关系。",
        },
        "previous_chapters": previous_chapters,
        "confirmed_timeline": [
            _non_empty_fields(item, ("chapter_no", "title", "description", "event", "time"))
            for item in (base_context.get("confirmed_timeline") or [])[-8:]
        ],
        "open_foreshadows": [
            _non_empty_fields(item, ("clue", "kind", "evidence", "expected_reveal_chapter"))
            for item in (base_context.get("open_foreshadows") or [])[:6]
        ],
        "active_factions": [
            _non_empty_fields(item, ("name", "description", "state"))
            for item in (base_context.get("active_factions") or [])
            if not faction_names or item.get("name") in faction_names
        ],
        "active_goals": [
            _non_empty_fields(item, ("title", "details", "progress", "priority"))
            for item in (base_context.get("active_goals") or [])[:6]
        ],
        "next_chapter_boundary": _next_boundary(
            base_context.get("next_chapter_boundary") or None
        ),
        "continuity_warnings": base_context.get("continuity_warnings") or [],
        "fact_version": base_context.get("fact_version", 0),
        "plan_version": base_context.get("plan_version", 0),
        "outline_version": plan.get("outline_version", 0),
        "excluded": base_context.get("excluded") or [],
    }


def build_chapter_generation_context(work: dict[str, Any], chapter_no: int) -> dict[str, Any]:
    """Build exactly the compact context shown in preview and sent to chapter generation."""
    return chapter_generation_context(build_context(work, chapter_no))


def build_context(work: dict[str, Any], chapter_no: int, recent_limit: int = 2) -> dict[str, Any]:
    """Return only facts available immediately before ``chapter_no``.

    Preview and generation both use this object, eliminating a hidden prompt
    context that could diverge from what the author sees.
    """
    plans = work.get("chapter_plans") or []
    plan = next((item for item in plans if int(item.get("chapter_no") or 0) == chapter_no), None) or {
        "chapter_no": chapter_no, "title": f"第{chapter_no}章"
    }
    canonical_state = get_story_state(work["id"], before_chapter=chapter_no)
    day = plan.get("story_day")
    day = int(day) if day is not None else None
    phase = next((item for item in (work.get("story_phases") or []) if item.get("phase_key") == plan.get("phase_key")), None)
    warnings: list[str] = []
    exclusions: list[dict[str, Any]] = []
    characters: list[dict[str, Any]] = []

    for character in work.get("characters") or []:
        character_id = str(character.get("id") or "")
        confirmed = dict((canonical_state.get("characters") or {}).get(character_id) or {})
        if str(character.get("dynamic_scope") or "legacy_unscoped") == "legacy_unscoped":
            exclusions.append({
                "kind": "legacy_dynamic_card", "character_id": character_id,
                "fields": ["status", "knowledge", "relationships"],
                "reason": "旧人物卡动态字段尚未归属到故事时间，不能作为当前事实。",
            })
        characters.append({
            **_long_term_character(character),
            "confirmed_state": confirmed,
            "knowledge_scope": confirmed.get("knowledge", []),
            "state_as_of_chapter": int((canonical_state.get("character_sources") or {}).get(character_id) or 0),
        })

    active_factions = []
    for faction in work.get("factions") or []:
        if _is_active_faction(faction, day):
            active_factions.append({key: faction.get(key) for key in ("id", "name", "description", "state", "formed_day", "active_from_day")})
        else:
            exclusions.append({
                "kind": "future_faction", "name": faction.get("name", ""),
                "reason": "势力尚未在本章故事日成立或公开活动。",
            })
    active_goals = [
        {key: goal.get(key) for key in ("id", "owner_type", "owner_id", "title", "status", "priority", "details", "progress")}
        for goal in work.get("goals") or []
        if goal.get("status") in {"active", "in_progress", "planned", "paused", "suspended"}
        and (goal.get("started_day") is None or day is None or int(goal["started_day"]) <= day)
        and (goal.get("ended_day") is None or day is None or int(goal["ended_day"]) >= day)
    ]
    for item in work.get("future_plans") or []:
        exclusions.append({
            "kind": "future_plan", "plan_type": item.get("plan_type", ""),
            "reason": "未来规划仅用于大纲约束，不作为正文当前事实。",
        })
    confirmed_timeline = [
        item for item in (work.get("timeline_events") or [])
        if item.get("review_status", "confirmed") == "confirmed"
        and item.get("source_extraction_status", "applied") in {"applied", ""}
        and int(item.get("chapter_no") or 0) < chapter_no
    ][-20:]
    open_foreshadows = [
        item for item in (work.get("foreshadows") or [])
        if item.get("status", "open") == "open" and int(item.get("planted_chapter") or 0) < chapter_no
    ]
    previous = [item for item in (work.get("chapters") or []) if int(item.get("chapter_no") or 0) < chapter_no][-recent_limit:]
    previous_plans = [item for item in plans if int(item.get("chapter_no") or 0) < chapter_no][-recent_limit:]
    next_plan = next((item for item in plans if int(item.get("chapter_no") or 0) == chapter_no + 1), None)
    if plan.get("fact_version") and int(plan["fact_version"]) != int(work.get("fact_version") or 0):
        warnings.append("本章大纲依赖的事实版本已变化；生成前应复核承接关系。")

    return {
        "long_term_rules": work.get("story_bible") or {},
        "long_term_facts": work.get("long_term_facts") or [],
        "chapter_plan": plan,
        "previous_plans": previous_plans,
        "next_chapter_boundary": next_plan or {},
        "story_phase": phase or {},
        "active_factions": active_factions,
        "active_goals": active_goals,
        "characters": characters,
        "open_foreshadows": open_foreshadows,
        "confirmed_timeline": confirmed_timeline,
        "previous_chapters": [_chapter_text(item) for item in previous],
        "fact_version": int(canonical_state.get("fact_version") or work.get("fact_version") or 0),
        "plan_version": int(plan.get("version") or 0),
        "excluded": exclusions,
        "retrieval_policy": {
            "legacy_unscoped_dynamic_card": "excluded",
            "future_plans": "excluded",
            "unconfirmed_state_extractions": "excluded",
            "max_previous_chapters": recent_limit,
        },
        "continuity_warnings": warnings,
    }


def record_context_audit(work_id: str, chapter_no: int, purpose: str, context: dict[str, Any]) -> str:
    """Persist exactly the context sent to a model for later comparison."""
    audit_id = str(uuid4())
    with transaction() as conn:
        conn.execute(
            """INSERT INTO context_audits(id,work_id,chapter_no,purpose,fact_version,outline_version,context_json,created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
             (audit_id, work_id, chapter_no, purpose, int(context.get("fact_version") or 0),
              int(context.get("outline_version") or (context.get("chapter_contract") or context.get("chapter_plan") or {}).get("outline_version") or 0), json_dumps(context), now_iso()),
        )
    return audit_id


def latest_context_audit(work_id: str, chapter_no: int, purpose: str) -> dict[str, Any] | None:
    with transaction() as conn:
        row = conn.execute(
            "SELECT * FROM context_audits WHERE work_id=? AND chapter_no=? AND purpose=? ORDER BY created_at DESC LIMIT 1",
            (work_id, chapter_no, purpose),
        ).fetchone()
        if not row:
            return None
        item = dict(row)
        item["context"] = json_loads(item.pop("context_json"), {})
        return item
