"""Persistence and dependency rules for the staged story-planning wizard."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from app.db import transaction
from app.services.character_cards import planning_character
from app.services.planning_quality import (
    evaluate_setup,
    planning_checks,
    planning_consistency_checks,
    planning_coverage_checks,
)
from app.services.repository import save_story_setup
from app.utils import json_dumps, json_loads, now_iso


STEP_DEFINITIONS = [
    ("contract", "创作契约", "先确认读者要获得什么体验"),
    ("setting", "核心设定", "确认金手指、规则和核心冲突"),
    ("protagonist", "主角小传", "单独确认主角的目标、原则和行为方式"),
    ("cast_roster", "角色阵容", "先确认配角和对手名单"),
    ("character", "人物小传", "逐个生成和确认人物小传"),
    ("arc", "卷级主线", "逐卷确认目标、升级和回报"),
    ("summary", "总梗概", "只整合已经确认的内容"),
]
STEP_ORDER = [item[0] for item in STEP_DEFINITIONS]
DEPENDENTS = {
    "contract": {"setting", "protagonist", "cast_roster", "character", "arc", "summary"},
    "setting": {"protagonist", "cast_roster", "character", "arc", "summary"},
    "protagonist": {"cast_roster", "character", "arc", "summary"},
    "cast_roster": {"character", "arc", "summary"},
    "character": {"arc", "summary"},
    "arc": {"summary"},
    "summary": set(),
}


def _decode(row: Any) -> dict[str, Any]:
    item = dict(row)
    item["content"] = json_loads(item.pop("content_json", "{}"), {})
    item["checks"] = json_loads(item.pop("checks_json", "{}"), {})
    item["parent_versions"] = json_loads(item.pop("parent_versions_json", "{}"), {})
    return item


def ensure_session(conn, work_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM planning_sessions WHERE work_id=?", (work_id,)).fetchone()
    if row:
        return dict(row)
    now = now_iso()
    session_id = str(uuid4())
    conn.execute(
        """
        INSERT INTO planning_sessions(id, work_id, status, current_step, preset, created_at, updated_at)
        VALUES (?, ?, 'in_progress', 'contract', 'custom', ?, ?)
        """,
        (session_id, work_id, now, now),
    )
    return dict(conn.execute("SELECT * FROM planning_sessions WHERE id=?", (session_id,)).fetchone())


def _required_character_keys(artifacts: list[dict[str, Any]]) -> list[str]:
    """Return the character biography keys promised by the confirmed roster."""
    roster = next(
        (
            item
            for item in artifacts
            if item["step"] == "cast_roster" and item["status"] == "confirmed"
        ),
        None,
    )
    if not roster:
        return []
    protagonist = next(
        (
            item
            for item in artifacts
            if item["step"] == "protagonist" and item["status"] == "confirmed"
        ),
        None,
    )
    protagonist_content = (protagonist or {}).get("content", {})
    protagonist_character = _artifact_section(protagonist_content, "character") if isinstance(protagonist_content, dict) else {}
    protagonist_name = _normalized_character_name(protagonist_character.get("name")) if isinstance(protagonist_character, dict) else ""
    characters = roster.get("content", {}).get("characters", [])
    return [
        str(item.get("item_key"))
        for item in characters
        if isinstance(item, dict)
        and item.get("item_key")
        and (not protagonist_name or _normalized_character_name(item.get("name")) != protagonist_name)
    ]


def _normalized_character_name(value: Any) -> str:
    return "".join(char for char in str(value or "").strip() if char.isalnum() or "\u4e00" <= char <= "\u9fff").casefold()


def _step_status(items: list[dict[str, Any]], *, required_keys: list[str] | None = None) -> str:
    if not items:
        return "not_started"
    if any(item["status"] == "stale" for item in items):
        return "stale"
    if required_keys is not None:
        statuses = {item["item_key"]: item["status"] for item in items}
        return "confirmed" if required_keys and all(statuses.get(key) == "confirmed" for key in required_keys) else "draft"
    if all(item["status"] == "confirmed" for item in items):
        return "confirmed"
    return "draft"


def _current_step(artifacts: list[dict[str, Any]]) -> str:
    required_character_keys = _required_character_keys(artifacts)
    for step in STEP_ORDER:
        items = [item for item in artifacts if item["step"] == step]
        required_keys = required_character_keys if step == "character" else None
        if _step_status(items, required_keys=required_keys) != "confirmed":
            return step
    return "complete"


def get_planning_session(work_id: str) -> dict[str, Any]:
    with transaction() as conn:
        session = ensure_session(conn, work_id)
        rows = conn.execute(
            "SELECT * FROM planning_artifacts WHERE session_id=? ORDER BY step, item_key",
            (session["id"],),
        ).fetchall()
        artifacts = [_decode(row) for row in rows]
        current = _current_step(artifacts)
        if session["current_step"] != current:
            conn.execute(
                "UPDATE planning_sessions SET current_step=?, updated_at=? WHERE id=?",
                (current, now_iso(), session["id"]),
            )
            session["current_step"] = current
        steps = []
        required_character_keys = _required_character_keys(artifacts)
        for key, label, description in STEP_DEFINITIONS:
            items = [item for item in artifacts if item["step"] == key]
            required_keys = required_character_keys if key == "character" else None
            steps.append({"step": key, "label": label, "description": description, "status": _step_status(items, required_keys=required_keys), "items": items})
        result = dict(session)
        result["steps"] = steps
        result["artifacts"] = artifacts
        result["usage"] = {
            "input_tokens": int(session.get("input_tokens") or 0),
            "output_tokens": int(session.get("output_tokens") or 0),
            "total_tokens": int(session.get("total_tokens") or 0),
            "known": bool(artifacts) and all(item.get("total_tokens") is not None for item in artifacts),
        }
        work = conn.execute(
            "SELECT estimated_words, average_chapter_words, target_chapter_count FROM works WHERE id=?",
            (work_id,),
        ).fetchone()
        planning_context: dict[str, list[dict[str, Any]]] = {}
        for item in artifacts:
            if item["status"] == "stale":
                continue
            planning_context.setdefault(item["step"], []).append(item["content"])
        result["coverage_checks"] = planning_coverage_checks(dict(work or {}), planning_context)
        return result


def confirmed_context(work_id: str) -> dict[str, Any]:
    with transaction() as conn:
        session = ensure_session(conn, work_id)
        rows = conn.execute(
            "SELECT * FROM planning_artifacts WHERE session_id=? AND status='confirmed' ORDER BY step, item_key",
            (session["id"],),
        ).fetchall()
        result: dict[str, Any] = {}
        for row in rows:
            item = _decode(row)
            result.setdefault(item["step"], []).append(item["content"])
        return result


def _confirmed_artifacts(work_id: str) -> list[dict[str, Any]]:
    with transaction() as conn:
        session = ensure_session(conn, work_id)
        rows = conn.execute(
            "SELECT * FROM planning_artifacts WHERE session_id=? AND status='confirmed' ORDER BY step, item_key",
            (session["id"],),
        ).fetchall()
    return [_decode(row) for row in rows]


def _artifact_contents(artifacts: list[dict[str, Any]], step: str, item_key: str | None = None) -> list[dict[str, Any]]:
    return [
        item["content"]
        for item in artifacts
        if item["step"] == step and (item_key is None or item["item_key"] == item_key)
    ]


def _selected_contract(content: dict[str, Any]) -> dict[str, Any]:
    selected = content.get("selected")
    if isinstance(selected, dict):
        return dict(selected)
    # Keep old confirmed sessions usable while still projecting only one option.
    candidates = content.get("candidates")
    if isinstance(candidates, list):
        return next((dict(item) for item in candidates if isinstance(item, dict)), {})
    return {}


def _summary_contract(contract: dict[str, Any]) -> dict[str, Any]:
    """Keep the selected contract while rejecting character-detail leakage."""
    return {
        key: value
        for key, value in contract.items()
        if key not in {"appearance", "voice", "facets"} and value not in (None, "", [], {})
    }


def _non_empty_mapping(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if item not in (None, "", [], {})}


def _story_bible_context(content: dict[str, Any]) -> dict[str, Any]:
    bible = _artifact_section(content, "story_bible")
    if not isinstance(bible, dict):
        return {}
    return _non_empty_mapping({
        key: bible.get(key)
        for key in ("core_hook", "core_conflict", "world", "stakes", "ending", "must_have_elements", "avoid_drift")
    })


def _character_context(content: dict[str, Any]) -> dict[str, Any]:
    character = _artifact_section(content, "character")
    return _non_empty_mapping(planning_character(character if isinstance(character, dict) else {}))


def _protagonist_summary(content: dict[str, Any]) -> dict[str, Any]:
    character = _artifact_section(content, "character")
    if not isinstance(character, dict):
        return {}
    core = character.get("dramatic_core") if isinstance(character.get("dramatic_core"), dict) else {}
    return _non_empty_mapping({
        "name": character.get("name"),
        "role": character.get("role"),
        "goal": core.get("goal") or character.get("goal"),
        "motivation": core.get("motivation") or character.get("motivation"),
        "arc": character.get("arc") or character.get("character_arc"),
    })


def _roster_context(content: dict[str, Any]) -> list[dict[str, Any]]:
    characters = content.get("characters") if isinstance(content.get("characters"), list) else []
    return [
        item
        for item in (
            _non_empty_mapping({
                "item_key": character.get("item_key"),
                "name": character.get("name"),
                "role": character.get("role"),
                "story_function": character.get("story_function"),
                "relationship": character.get("relationship_to_protagonist") or character.get("relationship"),
            })
            for character in characters
            if isinstance(character, dict)
        )
        if item
    ]


def _arc_context(content: dict[str, Any]) -> dict[str, Any]:
    arc = _artifact_section(content, "arc")
    if not isinstance(arc, dict):
        return {}
    return _non_empty_mapping({
        "title": arc.get("title"),
        "sequence": arc.get("sequence"),
        "goal": arc.get("goal"),
        "turning_point": arc.get("turning_point"),
        "ending_state": arc.get("ending_state"),
        "synopsis": arc.get("synopsis"),
    })


def _build_planning_context(artifacts: list[dict[str, Any]], step: str, item_key: str | None) -> dict[str, Any]:
    """Project only the confirmed facts needed by one planning step.

    Artifacts remain lossless in storage and ``confirmed_context`` stays available
    for compatibility.  Model prompts use this step-specific projection instead,
    so candidate wrappers and presentation-only character fields cannot leak
    into later planning steps.
    """
    context: dict[str, Any] = {}
    contract = _selected_contract((_artifact_contents(artifacts, "contract") or [{}])[0])
    if step == "summary":
        contract = _summary_contract(contract)
    if step in {"setting", "protagonist", "cast_roster", "character", "arc", "summary"} and contract:
        context["contract"] = [{"selected": contract}]

    setting_content = (_artifact_contents(artifacts, "setting") or [{}])[0]
    if step in {"protagonist", "cast_roster", "character", "arc", "summary"}:
        setting = _story_bible_context(setting_content)
        if setting:
            context["setting"] = [{"story_bible": setting}]

    protagonist_content = (_artifact_contents(artifacts, "protagonist") or [{}])[0]
    if step in {"cast_roster", "character", "arc"}:
        protagonist = _character_context(protagonist_content)
        if protagonist:
            context["protagonist"] = [{"character": protagonist}]
    elif step == "summary":
        protagonist = _protagonist_summary(protagonist_content)
        if protagonist:
            context["protagonist"] = [{"character": protagonist}]

    roster_content = (_artifact_contents(artifacts, "cast_roster") or [{}])[0]
    if step in {"character", "arc", "summary"}:
        roster = _roster_context(roster_content)
        if roster:
            context["cast_roster"] = [{"characters": roster}]

    if step == "character":
        cards = []
        for artifact in artifacts:
            if artifact["step"] != "character" or (item_key and artifact["item_key"] == item_key):
                continue
            card = _character_context(artifact["content"])
            if card:
                cards.append({"character": card})
        if cards:
            context["character"] = cards
    elif step == "arc":
        cards = []
        for artifact in artifacts:
            if artifact["step"] == "character":
                card = _character_context(artifact["content"])
                if card:
                    cards.append({"character": card})
        if cards:
            context["character"] = cards

    if step in {"arc", "summary"}:
        arcs = []
        for artifact in artifacts:
            if artifact["step"] != "arc" or (step == "arc" and item_key and artifact["item_key"] == item_key):
                continue
            arc = _arc_context(artifact["content"])
            if arc:
                arcs.append({"arc": arc})
        if arcs:
            context["arc"] = arcs
    return context


def planning_context_for_step(work_id: str, step: str, item_key: str | None = None) -> dict[str, Any]:
    """Return the minimal confirmed context required for one planning step."""
    if step not in STEP_ORDER:
        raise ValueError(f"不支持的规划步骤：{step}")
    return _build_planning_context(_confirmed_artifacts(work_id), step, item_key)


def _parent_versions(conn, session_id: str) -> dict[str, int]:
    rows = conn.execute(
        "SELECT step, MAX(version) AS version FROM planning_artifacts WHERE session_id=? AND status='confirmed' GROUP BY step",
        (session_id,),
    ).fetchall()
    return {row["step"]: int(row["version"] or 0) for row in rows}


def upsert_artifact(
    work_id: str,
    step: str,
    item_key: str,
    content: dict[str, Any],
    *,
    source: str = "model",
    feedback: str = "",
    checks: dict[str, Any] | None = None,
    usage: dict[str, Any] | None = None,
    model: str = "",
) -> dict[str, Any]:
    if step not in STEP_ORDER:
        raise ValueError(f"不支持的规划步骤：{step}")
    checks = checks or {}
    usage = usage or {}
    with transaction() as conn:
        session = ensure_session(conn, work_id)
        existing = conn.execute(
            "SELECT * FROM planning_artifacts WHERE session_id=? AND step=? AND item_key=?",
            (session["id"], step, item_key),
        ).fetchone()
        now = now_iso()
        version = int(existing["version"] or 0) + 1 if existing else 1
        values = (
            json_dumps(content), "draft", version, source, feedback, json_dumps(checks),
            json_dumps(_parent_versions(conn, session["id"])), usage.get("input_tokens"),
            usage.get("output_tokens"), usage.get("total_tokens"), model, now,
        )
        if existing:
            conn.execute(
                """
                UPDATE planning_artifacts SET content_json=?, status=?, version=?, source=?, feedback=?,
                    checks_json=?, parent_versions_json=?, input_tokens=?, output_tokens=?, total_tokens=?,
                    model=?, updated_at=?, confirmed_at=NULL WHERE id=?
                """,
                (*values, existing["id"]),
            )
            artifact_id = existing["id"]
        else:
            artifact_id = str(uuid4())
            conn.execute(
                """
                INSERT INTO planning_artifacts(
                    id, session_id, step, item_key, content_json, status, version, source, feedback,
                    checks_json, parent_versions_json, input_tokens, output_tokens, total_tokens, model,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (artifact_id, session["id"], step, item_key, *values[:-1], now, now),
            )
        total_known = all(value is not None for value in (usage.get("input_tokens"), usage.get("output_tokens"), usage.get("total_tokens")))
        if total_known:
            conn.execute(
                """
                UPDATE planning_sessions SET input_tokens=input_tokens+?, output_tokens=output_tokens+?,
                    total_tokens=total_tokens+?, updated_at=? WHERE id=?
                """,
                (int(usage["input_tokens"]), int(usage["output_tokens"]), int(usage["total_tokens"]), now, session["id"]),
            )
        conn.execute("UPDATE planning_sessions SET current_step=?, updated_at=? WHERE id=?", (step, now, session["id"]))
        row = conn.execute("SELECT * FROM planning_artifacts WHERE id=?", (artifact_id,)).fetchone()
        conn.execute(
            """
            INSERT INTO planning_artifact_snapshots(
                id, session_id, artifact_id, step, item_key, content_json, status, version,
                source, feedback, checks_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid4()), session["id"], artifact_id, step, item_key,
                row["content_json"], row["status"], row["version"], row["source"],
                row["feedback"], row["checks_json"], now,
            ),
        )
        return _decode(row)


def update_artifact_content(work_id: str, step: str, item_key: str, content: dict[str, Any], feedback: str = "") -> dict[str, Any]:
    checks = planning_checks(step, content, planning_context_for_step(work_id, step, item_key))
    return upsert_artifact(work_id, step, item_key, content, source="manual", feedback=feedback, checks=checks)


def list_planning_snapshots(work_id: str, step: str, item_key: str, limit: int = 20) -> list[dict[str, Any]]:
    """Return recent saved versions for one editable planning artifact."""
    safe_limit = max(1, min(int(limit), 100))
    with transaction() as conn:
        session = ensure_session(conn, work_id)
        rows = conn.execute(
            """
            SELECT id, artifact_id, step, item_key, content_json, status, version, source,
                   feedback, checks_json, created_at
            FROM planning_artifact_snapshots
            WHERE session_id=? AND step=? AND item_key=?
            ORDER BY created_at DESC, rowid DESC
            LIMIT ?
            """,
            (session["id"], step, item_key, safe_limit),
        ).fetchall()
    snapshots = []
    for row in rows:
        item = dict(row)
        item["content"] = json_loads(item.pop("content_json"), {})
        item["checks"] = json_loads(item.pop("checks_json"), {})
        snapshots.append(item)
    return snapshots


def restore_planning_snapshot(work_id: str, snapshot_id: str) -> dict[str, Any]:
    """Restore a snapshot as a new draft version, keeping the current draft recoverable."""
    with transaction() as conn:
        session = ensure_session(conn, work_id)
        row = conn.execute(
            "SELECT * FROM planning_artifact_snapshots WHERE id=? AND session_id=?",
            (snapshot_id, session["id"]),
        ).fetchone()
    if not row:
        raise ValueError("规划版本快照不存在")
    content = json_loads(row["content_json"], {})
    if not isinstance(content, dict):
        raise ValueError("规划版本快照内容无效")
    update_artifact_content(work_id, row["step"], row["item_key"], content, row["feedback"])
    return get_planning_session(work_id)


def _invalidate_descendants(conn, session_id: str, step: str) -> None:
    descendants = DEPENDENTS.get(step, set())
    if not descendants:
        return
    placeholders = ",".join("?" for _ in descendants)
    conn.execute(
        f"UPDATE planning_artifacts SET status='stale', updated_at=? WHERE session_id=? AND step IN ({placeholders}) AND status='confirmed'",
        (now_iso(), session_id, *sorted(descendants)),
    )


def confirm_artifact(work_id: str, step: str, item_key: str) -> dict[str, Any]:
    with transaction() as conn:
        session = ensure_session(conn, work_id)
        row = conn.execute(
            "SELECT * FROM planning_artifacts WHERE session_id=? AND step=? AND item_key=?",
            (session["id"], step, item_key),
        ).fetchone()
        if not row:
            raise ValueError("当前步骤还没有可确认的草稿")
        content = json_loads(row["content_json"], {})
        if not content:
            raise ValueError("当前步骤内容为空，不能确认")
        artifacts = [
            _decode(item)
            for item in conn.execute(
                "SELECT * FROM planning_artifacts WHERE session_id=? AND status='confirmed'",
                (session["id"],),
            ).fetchall()
        ]
        checks = planning_checks(step, content, _build_planning_context(artifacts, step, item_key))
        if checks["blocking"]:
            prefix = "角色阵容不能确认：" if step == "cast_roster" else "当前步骤不能确认："
            raise ValueError(prefix + "；".join(checks["blocking"]))
        was_confirmed = row["status"] == "confirmed"
        conn.execute(
            "UPDATE planning_artifacts SET status='confirmed', confirmed_at=?, updated_at=? WHERE id=?",
            (now_iso(), now_iso(), row["id"]),
        )
        if not was_confirmed:
            _invalidate_descendants(conn, session["id"], step)
        conn.execute(
            "UPDATE planning_sessions SET current_step=?, updated_at=? WHERE id=?",
            (_current_step([_decode(item) for item in conn.execute("SELECT * FROM planning_artifacts WHERE session_id=?", (session["id"],)).fetchall()]), now_iso(), session["id"]),
        )
    return get_planning_session(work_id)


def prerequisite_error(work_id: str, step: str) -> str | None:
    session = get_planning_session(work_id)
    statuses = {item["step"]: item["status"] for item in session["steps"]}
    required = {
        "contract": [],
        "setting": ["contract"],
        "protagonist": ["contract", "setting"],
        "cast_roster": ["contract", "protagonist"],
        "character": ["contract", "protagonist", "cast_roster"],
        "arc": ["contract", "setting", "protagonist", "cast_roster", "character"],
        "summary": ["contract", "setting", "protagonist", "cast_roster", "character", "arc"],
    }.get(step, [])
    missing = [item for item in required if statuses.get(item) != "confirmed"]
    if missing:
        labels = {key: label for key, label, _ in STEP_DEFINITIONS}
        return "请先确认：" + "、".join(labels[item] for item in missing)
    return None


def _all_ready(session: dict[str, Any]) -> bool:
    statuses = {item["step"]: item["status"] for item in session["steps"]}
    if any(statuses.get(step) != "confirmed" for step in ("contract", "setting", "protagonist", "cast_roster", "summary")):
        return False
    character_items = next((item["items"] for item in session["steps"] if item["step"] == "character"), [])
    arc_items = next((item["items"] for item in session["steps"] if item["step"] == "arc"), [])
    return bool(character_items) and all(item["status"] == "confirmed" for item in character_items) and bool(arc_items) and all(item["status"] == "confirmed" for item in arc_items)


def _artifact_section(content: dict[str, Any], section: str) -> Any:
    """Read a planning section from both canonical and candidate-wrapped drafts."""
    direct = content.get(section)
    if direct:
        return direct
    candidates = []
    if isinstance(content.get("selected"), dict):
        candidates.append(content["selected"])
    if isinstance(content.get("candidates"), list):
        candidates.extend(item for item in content["candidates"] if isinstance(item, dict))
    for candidate in candidates:
        value = candidate.get(section)
        if value:
            return value
    return {} if section != "characters" else []


def _deduplicate_final_characters(
    protagonist: dict[str, Any], side_entries: list[tuple[str, dict[str, Any]]]
) -> list[dict[str, Any]]:
    """Deduplicate stable identities/names while rejecting duplicate protagonists."""
    entries = [(str(protagonist.get("id") or "protagonist"), protagonist, True), *[
        (str(character.get("id") or item_key), character, False)
        for item_key, character in side_entries
    ]]
    result: list[dict[str, Any]] = []
    by_identity: dict[str, tuple[dict[str, Any], bool]] = {}
    by_name: dict[str, tuple[dict[str, Any], bool]] = {}
    for identity, character, is_protagonist in entries:
        if not isinstance(character, dict):
            continue
        name = str(character.get("name") or "").strip()
        normalized_name = _normalized_character_name(name)
        existing_identity = by_identity.get(identity)
        if existing_identity:
            existing_character, existing_is_protagonist = existing_identity
            if _normalized_character_name(existing_character.get("name")) == normalized_name:
                if is_protagonist or existing_is_protagonist:
                    raise ValueError(f"最终人物表包含重复主角“{name or existing_character.get('name', '')}”，不能静默落库")
                continue
            raise ValueError(f"最终人物表中的稳定身份 {identity} 对应多个姓名，不能静默选择")
        existing_name = by_name.get(normalized_name) if normalized_name else None
        if existing_name:
            existing_character, existing_is_protagonist = existing_name
            if is_protagonist or existing_is_protagonist:
                raise ValueError(f"最终人物表包含重复主角“{name or existing_character.get('name', '')}”，不能静默落库")
            continue
        by_identity[identity] = (character, is_protagonist)
        if normalized_name:
            by_name[normalized_name] = (character, is_protagonist)
        result.append(character)
    return result


def _merge_unique_texts(*values: Any) -> list[str]:
    """Merge planning list fields in source order without losing later additions."""
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, list):
            continue
        for item in value:
            text = str(item or "").strip()
            if not text or text in seen:
                continue
            seen.add(text)
            result.append(text)
    return result


def finalize_planning(work_id: str) -> dict[str, Any]:
    session = get_planning_session(work_id)
    if not _all_ready(session):
        raise ValueError("故事档案仍有未确认或待复核步骤")
    consistency_checks = planning_consistency_checks(confirmed_context(work_id))
    if consistency_checks["blocking"]:
        raise ValueError("跨步骤一致性检查未通过：" + "；".join(consistency_checks["blocking"]))
    contents: dict[str, list[dict[str, Any]]] = {}
    for item in session["artifacts"]:
        if item["status"] == "confirmed":
            contents.setdefault(item["step"], []).append(item["content"])
    contract = (contents.get("contract") or [{}])[0]
    selected = contract.get("selected") or (contract.get("candidates") or [{}])[0]
    setting = _artifact_section((contents.get("setting") or [{}])[0], "story_bible")
    summary = _artifact_section((contents.get("summary") or [{}])[0], "story_bible")
    if not str(summary.get("summary") or "").strip():
        raise ValueError("总梗概已确认，但没有可写入故事档案的梗概内容，请重新生成或保存后再确认")
    bible = {**setting, **summary}
    bible.update({
        "summary": summary.get("summary", ""),
        "theme": summary.get("theme", ""),
        "style_rules": summary.get("style_rules", ""),
        "title_interpretation": selected.get("title_interpretation", ""),
        "reader_promise": selected.get("reader_promise", ""),
        "must_have_elements": _merge_unique_texts(
            selected.get("must_have_elements"),
            setting.get("must_have_elements"),
            summary.get("must_have_elements"),
        ),
        "avoid_drift": _merge_unique_texts(
            selected.get("avoid_drift"),
            setting.get("avoid_drift"),
            summary.get("avoid_drift"),
        ),
    })
    protagonist = _artifact_section((contents.get("protagonist") or [{}])[0], "character")
    protagonist_name = _normalized_character_name(protagonist.get("name"))
    roster_characters = _artifact_section((contents.get("cast_roster") or [{}])[0], "characters")
    if protagonist_name and any(
        isinstance(item, dict) and _normalized_character_name(item.get("name")) == protagonist_name
        for item in roster_characters
    ):
        raise ValueError(f"最终化前发现角色阵容包含重复主角“{protagonist.get('name', '')}”，不能静默落库")
    side_entries = [
        (item["item_key"], _artifact_section(item["content"], "character"))
        for item in session["artifacts"]
        if item["step"] == "character" and item["status"] == "confirmed"
    ]
    characters = _deduplicate_final_characters(protagonist, side_entries)
    arcs = [_artifact_section(item, "arc") for item in contents.get("arc", [])]
    with transaction() as conn:
        work_row = conn.execute("SELECT * FROM works WHERE id=?", (work_id,)).fetchone()
    coverage_checks = planning_coverage_checks(
        dict(work_row or {}),
        {"story_bible": bible, "plot_arcs": arcs},
    )
    if coverage_checks["blocking"]:
        raise ValueError("全书覆盖度检查未通过：" + "；".join(coverage_checks["blocking"]))
    quality_issues, quality_score = evaluate_setup(dict(work_row or {}), {
        "story_bible": bible,
        "characters": characters,
        "plot_arcs": arcs,
    })
    data = {
        "story_bible": bible,
        "characters": characters,
        "plot_arcs": arcs,
        "generation_source": "model",
        "quality_issues": quality_issues,
        "quality_score": quality_score,
        "consistency_checks": consistency_checks,
        "coverage_checks": coverage_checks,
    }
    with transaction() as conn:
        save_story_setup(conn, work_id, data)
        conn.execute("UPDATE planning_sessions SET status='completed', current_step='complete', updated_at=? WHERE work_id=?", (now_iso(), work_id))
        conn.execute("UPDATE works SET status='planning', updated_at=? WHERE id=?", (now_iso(), work_id))
    return data


def reset_planning(work_id: str) -> dict[str, Any]:
    with transaction() as conn:
        if conn.execute("SELECT 1 FROM chapters WHERE work_id=? LIMIT 1", (work_id,)).fetchone():
            raise ValueError("作品已经存在正文，不能直接清空故事规划")
        session = ensure_session(conn, work_id)
        conn.execute("DELETE FROM planning_artifacts WHERE session_id=?", (session["id"],))
        conn.execute(
            """
            UPDATE planning_sessions SET status='in_progress', current_step='contract', preset='custom',
                input_tokens=0, output_tokens=0, total_tokens=0, updated_at=? WHERE id=?
            """,
            (now_iso(), session["id"]),
        )
        conn.execute("UPDATE characters SET active=0 WHERE work_id=?", (work_id,))
        conn.execute("UPDATE plot_arcs SET active=0 WHERE work_id=?", (work_id,))
        conn.execute("DELETE FROM chapter_plans WHERE work_id=?", (work_id,))
        conn.execute(
            """
            UPDATE story_bibles SET summary='', theme='', world='', ending='', style_rules='',
                title_interpretation='', reader_promise='', core_hook='', core_conflict='', stakes='',
                must_have_elements_json='[]', avoid_drift_json='[]', generation_source='', quality_score=0,
                quality_issues_json='[]', locked=0, updated_at=? WHERE work_id=?
            """,
            (now_iso(), work_id),
        )
        conn.execute("UPDATE works SET status='draft', updated_at=? WHERE id=?", (now_iso(), work_id))
    return get_planning_session(work_id)
