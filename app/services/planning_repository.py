"""Persistence and dependency rules for the staged story-planning wizard."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from app.db import transaction
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
    characters = roster.get("content", {}).get("characters", [])
    return [
        str(item.get("item_key"))
        for item in characters
        if isinstance(item, dict) and item.get("item_key")
    ]


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
        return _decode(row)


def update_artifact_content(work_id: str, step: str, item_key: str, content: dict[str, Any], feedback: str = "") -> dict[str, Any]:
    return upsert_artifact(work_id, step, item_key, content, source="manual", feedback=feedback)


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
        if not json_loads(row["content_json"], {}):
            raise ValueError("当前步骤内容为空，不能确认")
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


def finalize_planning(work_id: str) -> dict[str, Any]:
    session = get_planning_session(work_id)
    if not _all_ready(session):
        raise ValueError("故事档案仍有未确认或待复核步骤")
    contents: dict[str, list[dict[str, Any]]] = {}
    for item in session["artifacts"]:
        if item["status"] == "confirmed":
            contents.setdefault(item["step"], []).append(item["content"])
    contract = (contents.get("contract") or [{}])[0]
    selected = contract.get("selected") or (contract.get("candidates") or [{}])[0]
    setting = (contents.get("setting") or [{}])[0].get("story_bible", {})
    summary = (contents.get("summary") or [{}])[0].get("story_bible", {})
    bible = {**setting, **summary}
    bible.update({
        "title_interpretation": selected.get("title_interpretation", bible.get("title_interpretation", "")),
        "reader_promise": selected.get("reader_promise", bible.get("reader_promise", "")),
        "style_rules": selected.get("style_rules", bible.get("style_rules", "")),
        "must_have_elements": selected.get("must_have_elements", bible.get("must_have_elements", [])),
        "avoid_drift": selected.get("avoid_drift", bible.get("avoid_drift", [])),
    })
    protagonist = (contents.get("protagonist") or [{}])[0].get("character", {})
    characters = [protagonist]
    characters.extend(item.get("character", {}) for item in contents.get("character", []))
    arcs = [item.get("arc", {}) for item in contents.get("arc", [])]
    data = {"story_bible": bible, "characters": characters, "plot_arcs": arcs, "generation_source": "model", "quality_issues": [], "quality_score": 0}
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
