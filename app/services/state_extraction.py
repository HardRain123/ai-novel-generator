"""Chapter versioning, state extraction and author-reviewed memory updates."""

import hashlib
from typing import Any
from uuid import uuid4

from app.config import LLM_API_KEY, LLM_MODEL
from app.db import transaction
from app.services.context_builder import build_context
from app.services.novel_engine import STATE_EXTRACTOR_VERSION, engine
from app.services.state_engine import rebuild_work_state, record_event
from app.services.state_engine import supersede_chapter_version
from app.utils import json_dumps, json_loads, now_iso

PROMPT_VERSION = "state-extraction-v2"


def _json_value(value: Any) -> str | None:
    return None if value is None else json_dumps(value)


def _resolve_character(conn, work_id: str, name: str):
    normalized = str(name).strip()
    row = conn.execute(
        "SELECT id, name FROM characters WHERE work_id = ? AND name = ? LIMIT 1",
        (work_id, normalized),
    ).fetchone()
    if row:
        return row["id"], row["name"]
    row = conn.execute(
        """
        SELECT c.id, c.name FROM character_aliases a
        JOIN characters c ON c.id = a.character_id
        WHERE a.work_id = ? AND a.alias = ? LIMIT 1
        """,
        (work_id, normalized),
    ).fetchone()
    return (row["id"], row["name"]) if row else (None, normalized)


def _chapter_version(conn, work_id: str, chapter: dict[str, Any], source: str) -> dict[str, Any]:
    content = str(chapter.get("content", ""))
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    existing = conn.execute(
        "SELECT * FROM chapter_versions WHERE work_id = ? AND chapter_no = ? AND content_hash = ? LIMIT 1",
        (work_id, int(chapter["chapter_no"]), content_hash),
    ).fetchone()
    if existing:
        old_versions = conn.execute(
            "SELECT id FROM chapter_versions WHERE work_id=? AND chapter_no=? AND is_current=1 AND id<>?",
            (work_id, int(chapter["chapter_no"]), existing["id"]),
        ).fetchall()
        conn.execute(
            "UPDATE chapter_versions SET is_current=0 WHERE work_id=? AND chapter_no=? AND id<>?",
            (work_id, int(chapter["chapter_no"]), existing["id"]),
        )
        conn.execute("UPDATE chapter_versions SET is_current=1,superseded_by=NULL,replaced_at=NULL WHERE id=?", (existing["id"],))
        conn.execute("UPDATE chapters SET current_version_id=? WHERE work_id=? AND chapter_no=?", (existing["id"], work_id, int(chapter["chapter_no"])))
        supersede_chapter_version(conn, work_id, int(chapter["chapter_no"]), [row["id"] for row in old_versions], existing["id"])
        return dict(existing)
    latest = conn.execute(
        "SELECT COALESCE(MAX(version_no), 0) AS version_no FROM chapter_versions WHERE work_id = ? AND chapter_no = ?",
        (work_id, int(chapter["chapter_no"])),
    ).fetchone()
    version = {
        "id": str(uuid4()),
        "work_id": work_id,
        "chapter_id": str(chapter.get("id") or ""),
        "chapter_no": int(chapter["chapter_no"]),
        "version_no": int(latest["version_no"] or 0) + 1,
        "content": content,
        "content_hash": content_hash,
        "source": source,
        "created_at": now_iso(),
    }
    old_versions = conn.execute(
        "SELECT id FROM chapter_versions WHERE work_id=? AND chapter_no=? AND is_current=1",
        (work_id, int(chapter["chapter_no"])),
    ).fetchall()
    conn.execute(
        "UPDATE chapter_versions SET is_current=0,superseded_by=?,replaced_at=? WHERE work_id=? AND chapter_no=? AND is_current=1",
        (version["id"], now_iso(), work_id, int(chapter["chapter_no"])),
    )
    conn.execute(
        """
        INSERT INTO chapter_versions(id, work_id, chapter_id, chapter_no, version_no, content, content_hash, source, created_at, is_current, fact_version)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0)
        """,
        tuple(version.values()),
    )
    conn.execute(
        "UPDATE chapters SET current_version_id=? WHERE work_id=? AND chapter_no=?",
        (version["id"], work_id, int(chapter["chapter_no"])),
    )
    supersede_chapter_version(conn, work_id, int(chapter["chapter_no"]), [row["id"] for row in old_versions], version["id"])
    return version


def _evidence(content: str, value: Any, warnings: list[str]) -> str:
    evidence = str(value or "").strip()
    if evidence and evidence in content:
        return evidence
    if evidence:
        warnings.append("模型返回的证据句未出现在本章正文中，已标记为空证据。")
    return ""


def _format_extraction(conn, extraction_id: str) -> dict[str, Any]:
    extraction = conn.execute(
        "SELECT * FROM state_extractions WHERE id = ? LIMIT 1", (extraction_id,)
    ).fetchone()
    if not extraction:
        return {}
    extraction_data = dict(extraction)
    extraction_data["raw"] = json_loads(extraction["raw_json"], {})
    extraction_data["characters"] = []
    rows = conn.execute(
        "SELECT * FROM character_state_changes WHERE extraction_id = ? ORDER BY created_at, id",
        (extraction_id,),
    ).fetchall()
    for row in rows:
        item = dict(row)
        item["old_value"] = json_loads(item.pop("old_value_json"), None)
        item["new_value"] = json_loads(item.pop("new_value_json"), None)
        item["reviewed_value"] = json_loads(item.pop("reviewed_value_json"), None)
        extraction_data["characters"].append(item)

    alias_rows = conn.execute(
        "SELECT * FROM character_alias_candidates WHERE extraction_id = ? ORDER BY created_at, id",
        (extraction_id,),
    ).fetchall()
    extraction_data["aliases"] = [dict(row) for row in alias_rows]

    event_rows = conn.execute(
        "SELECT * FROM timeline_events WHERE source_extraction_id = ? ORDER BY event_order, id",
        (extraction_id,),
    ).fetchall()
    extraction_data["timeline_events"] = []
    for row in event_rows:
        item = dict(row)
        item["participants"] = json_loads(item.pop("participants_json"), [])
        extraction_data["timeline_events"].append(item)
    extraction_data["foreshadows"] = [dict(row) for row in conn.execute(
        "SELECT * FROM foreshadow_candidates WHERE extraction_id=? ORDER BY created_at, id", (extraction_id,)
    ).fetchall()]
    return extraction_data


def _existing_extraction(conn, version_id: str):
    return conn.execute(
        """
        SELECT id FROM state_extractions
        WHERE chapter_version_id = ? AND extractor_version LIKE ?
        ORDER BY run_no DESC, created_at DESC LIMIT 1
        """,
        (version_id, f"{STATE_EXTRACTOR_VERSION}%"),
    ).fetchone()


def queue_pending_extraction(
    work: dict[str, Any],
    chapter: dict[str, Any],
    source: str = "generation",
    profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a lightweight pending record without waiting for the extractor model."""
    with transaction() as conn:
        version = _chapter_version(conn, work["id"], chapter, source)
        existing = _existing_extraction(conn, version["id"])
        if existing:
            return _format_extraction(conn, existing["id"])

        model_name = (profile or {}).get("model") or (LLM_MODEL if LLM_API_KEY else "fallback")
        return {
            "id": "",
            "work_id": work["id"],
            "chapter_version_id": version["id"],
            "status": "queued",
            "extractor_version": STATE_EXTRACTOR_VERSION,
            "run_no": 1,
            "prompt_version": PROMPT_VERSION,
            "model": model_name,
            "warning": "状态提取任务已排队，完成后可审核结果。",
            "characters": [],
            "aliases": [],
            "timeline_events": [],
            "foreshadows": [],
            "job_id": "",
        }


def extract_and_persist(
    work: dict[str, Any],
    chapter: dict[str, Any],
    source: str = "generation",
    force: bool = False,
    profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """提取一章状态并保存为 pending；只有审核后才会写入正式状态。"""
    with transaction() as conn:
        version = _chapter_version(conn, work["id"], chapter, source)
        existing = _existing_extraction(conn, version["id"])
        if existing and not force:
            return _format_extraction(conn, existing["id"])
        previous = existing["id"] if existing else None
        latest_run = conn.execute(
            "SELECT COALESCE(MAX(run_no), 0) AS run_no FROM state_extractions WHERE chapter_version_id = ?",
            (version["id"],),
        ).fetchone()["run_no"]

    result = engine.extract_state_changes(work, chapter, profile) if profile is not None else engine.extract_state_changes(work, chapter)
    model_name = (profile or {}).get("model") or (LLM_MODEL if LLM_API_KEY else "fallback")
    previous_state_by_name = {
        item.get("name"): item.get("confirmed_state", {})
        for item in build_context(work, int(chapter.get("chapter_no") or 0)).get("characters", [])
    }
    for character in result.get("characters", []):
        previous_state = previous_state_by_name.get(character.get("character_name"), {})
        for change in character.get("changes", []):
            old_value = change.get("old_value")
            if isinstance(old_value, str) and old_value.startswith("从 previous_confirmed_state"):
                old_value = None
            if old_value is None:
                old_value = previous_state.get(change.get("field"))
            change["old_value"] = old_value
    warnings = [str(item) for item in result.get("warnings", []) if str(item).strip()]
    content = str(chapter.get("content", ""))
    for character in result.get("characters", []):
        for change in character.get("changes", []):
            change["evidence"] = _evidence(content, change.get("evidence"), warnings)
    for event in result.get("timeline_events", []):
        event["evidence"] = _evidence(content, event.get("evidence"), warnings)
    for item in result.get("foreshadows", []):
        item["evidence"] = _evidence(content, item.get("evidence"), warnings)
    result["warnings"] = warnings

    run_no = int(latest_run or 0) + 1
    extractor_version = STATE_EXTRACTOR_VERSION if run_no == 1 else f"{STATE_EXTRACTOR_VERSION}.r{run_no}"
    with transaction() as conn:
        extraction_id = str(uuid4())
        if previous:
            conn.execute(
                "UPDATE state_extractions SET status='superseded', superseded_by=? WHERE id=? AND status IN ('pending', 'applied')",
                (extraction_id, previous),
            )
        conn.execute(
            """
            INSERT INTO state_extractions(
                id, work_id, chapter_version_id, status, extractor_version, run_no,
                prompt_version, model, raw_json, warning, created_at
            ) VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                extraction_id, work["id"], version["id"], extractor_version, run_no,
                PROMPT_VERSION, model_name, json_dumps(result), "；".join(warnings), now_iso(),
            ),
        )

        for character in result.get("characters", []):
            character_id, canonical_name = _resolve_character(conn, work["id"], character["character_name"])
            for alias in character.get("aliases", []):
                alias = str(alias).strip()
                if alias and alias != canonical_name:
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO character_alias_candidates(
                            id, work_id, extraction_id, character_id, character_name, alias, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (str(uuid4()), work["id"], extraction_id, character_id, canonical_name, alias, now_iso()),
                    )
            for change in character.get("changes", []):
                conn.execute(
                    """
                    INSERT INTO character_state_changes(
                        id, work_id, extraction_id, character_id, character_name, field,
                        old_value_json, new_value_json, evidence, confidence, status, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                    """,
                    (
                        str(uuid4()), work["id"], extraction_id, character_id,
                        character.get("character_name", canonical_name), change["field"],
                        _json_value(change.get("old_value")), _json_value(change.get("new_value")),
                        change.get("evidence", ""), change.get("confidence", 0.5), now_iso(),
                    ),
                )

        for index, event in enumerate(result.get("timeline_events", []), start=1):
            conn.execute(
                """
                INSERT INTO timeline_events(
                    id, work_id, event_order, chapter_no, title, description, created_at,
                    story_time_text, time_type, location, participants_json, evidence,
                    confidence, review_status, source_extraction_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    str(uuid4()), work["id"], int(chapter["chapter_no"]) * 1000 + index,
                    int(chapter["chapter_no"]), event["title"], event.get("description", ""),
                    now_iso(), event.get("story_time_text", ""), event.get("time_type", "unknown"),
                    event.get("location", ""), json_dumps(event.get("participants", [])),
                    event.get("evidence", ""), event.get("confidence", 0.5), extraction_id,
                ),
            )
        for item in result.get("foreshadows", []):
            conn.execute(
                """INSERT INTO foreshadow_candidates(id,work_id,extraction_id,clue,kind,planted_chapter,
                expected_reveal_chapter,evidence,confidence,status,created_at) VALUES (?,?,?,?,?,?,?,?,?,'pending',?)""",
                (str(uuid4()), work["id"], extraction_id, item["clue"], item.get("kind", "clue"),
                 item.get("planted_chapter", int(chapter["chapter_no"])), item.get("expected_reveal_chapter", 0),
                 item.get("evidence", ""), item.get("confidence", 0.5), now_iso()),
            )
        return _format_extraction(conn, extraction_id)


def _apply_character_change(conn, extraction: dict[str, Any], item: dict[str, Any], value: Any) -> None:
    character_id = item.get("character_id")
    if not character_id:
        raise ValueError(f"无法确认人物“{item.get('character_name', '')}”，不能应用状态变化")
    row = conn.execute(
        "SELECT state_json FROM character_states WHERE work_id = ? AND character_id = ? LIMIT 1",
        (extraction["work_id"], character_id),
    ).fetchone()
    state = json_loads(row["state_json"], {}) if row else {}
    state[item["field"]] = value
    chapter_no = conn.execute(
        "SELECT chapter_no FROM chapter_versions WHERE id = ? LIMIT 1",
        (extraction["chapter_version_id"],),
    ).fetchone()["chapter_no"]
    plan = conn.execute(
        "SELECT story_day FROM chapter_plans WHERE work_id=? AND chapter_no=?",
        (extraction["work_id"], chapter_no),
    ).fetchone()
    story_day = plan["story_day"] if plan else None
    now = now_iso()
    if row:
        conn.execute(
            """
            UPDATE character_states SET state_json=?, as_of_chapter=?, source_version_id=?, updated_at=?
            WHERE work_id=? AND character_id=?
            """,
            (json_dumps(state), chapter_no, extraction["chapter_version_id"], now, extraction["work_id"], character_id),
        )
    else:
        conn.execute(
            """
            INSERT INTO character_states(id, work_id, character_id, state_json, as_of_chapter, source_version_id, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (str(uuid4()), extraction["work_id"], character_id, json_dumps(state), chapter_no, extraction["chapter_version_id"], now),
        )
    record_event(
        conn, extraction["work_id"], chapter_no=chapter_no,
        chapter_version_id=extraction["chapter_version_id"], story_day=story_day,
        event_type="CHARACTER_STATE_CHANGED", entity_type="character", entity_id=character_id,
        before={item["field"]: json_loads(item.get("old_value_json"), None)},
        after={item["field"]: value}, evidence=item.get("evidence", ""),
        confidence=float(item.get("confidence") or 0),
        risk_level="high" if item.get("field") in {"physical_state", "secret_exposed", "faction"} else "low",
    )
    rebuild_work_state(conn, extraction["work_id"], chapter_no)
    conn.execute(
        """UPDATE chapter_plans SET stale_reason=?
           WHERE work_id=? AND chapter_no>? AND stale_reason=''""",
        (f"第{chapter_no}章已确认{item.get('character_name', '人物')}的{item.get('field', '状态')}变化，请复核承接", extraction["work_id"], chapter_no),
    )


def review_extraction(work_id: str, extraction_id: str, items: list[dict[str, Any]]) -> dict[str, Any] | None:
    """批量审核提取结果，并在同一事务内应用已接受的正式状态。"""
    with transaction() as conn:
        extraction_row = conn.execute(
            "SELECT * FROM state_extractions WHERE id=? AND work_id=? LIMIT 1",
            (extraction_id, work_id),
        ).fetchone()
        if not extraction_row:
            return None
        extraction = dict(extraction_row)
        if extraction["status"] in {"superseded", "applied", "rejected"}:
            raise ValueError("该提取结果已完成审核或已失效，不能再次审核")

        for review in items:
            kind = review.get("kind")
            item_id = review.get("id")
            action = review.get("action")
            if action not in {"accept", "reject"}:
                raise ValueError("审核动作必须是 accept 或 reject")
            if kind == "character":
                row = conn.execute(
                    "SELECT * FROM character_state_changes WHERE id=? AND extraction_id=? LIMIT 1",
                    (item_id, extraction_id),
                ).fetchone()
                if not row:
                    raise ValueError("角色状态变化不存在")
                item = dict(row)
                if item["status"] != "pending":
                    raise ValueError("角色状态变化已经审核过")
                value = review.get("edited_value") if review.get("edited_value") is not None else json_loads(item["new_value_json"], None)
                if action == "accept":
                    _apply_character_change(conn, extraction, item, value)
                conn.execute(
                    "UPDATE character_state_changes SET status=?, reviewed_value_json=?, reviewed_at=? WHERE id=?",
                    ("accepted" if action == "accept" else "rejected", _json_value(value), now_iso(), item_id),
                )
            elif kind == "timeline":
                row = conn.execute(
                    "SELECT * FROM timeline_events WHERE id=? AND source_extraction_id=? LIMIT 1",
                    (item_id, extraction_id),
                ).fetchone()
                if not row:
                    raise ValueError("时间线候选不存在")
                if row["review_status"] != "pending":
                    raise ValueError("时间线候选已经审核过")
                edited = review.get("edited_value") if isinstance(review.get("edited_value"), dict) else {}
                if action == "accept":
                    allowed = {"title", "description", "story_time_text", "time_type", "location", "evidence", "confidence"}
                    updates = {key: edited[key] for key in allowed if key in edited}
                    if updates:
                        assignments = ", ".join(f"{key}=?" for key in updates)
                        conn.execute(f"UPDATE timeline_events SET {assignments} WHERE id=?", (*updates.values(), item_id))
                conn.execute(
                    "UPDATE timeline_events SET review_status=? WHERE id=?",
                    ("confirmed" if action == "accept" else "rejected", item_id),
                )
                if action == "accept":
                    chapter_no = int(row["chapter_no"] or 0)
                    plan = conn.execute(
                        "SELECT story_day FROM chapter_plans WHERE work_id=? AND chapter_no=? LIMIT 1",
                        (work_id, chapter_no),
                    ).fetchone()
                    record_event(
                        conn, work_id, chapter_no=chapter_no,
                        chapter_version_id=extraction["chapter_version_id"],
                        story_day=plan["story_day"] if plan else None,
                        event_type="TIMELINE_EVENT_CONFIRMED", entity_type="timeline_event", entity_id=item_id,
                        before={}, after={
                            "title": row["title"], "description": row["description"],
                            "location": row["location"], "participants": json_loads(row["participants_json"], []),
                        }, evidence=row["evidence"], confidence=float(row["confidence"] or 0),
                    )
                    rebuild_work_state(conn, work_id, chapter_no)
            elif kind == "alias":
                row = conn.execute(
                    "SELECT * FROM character_alias_candidates WHERE id=? AND extraction_id=? LIMIT 1",
                    (item_id, extraction_id),
                ).fetchone()
                if not row:
                    raise ValueError("人物别名候选不存在")
                if row["status"] != "pending":
                    raise ValueError("人物别名候选已经审核过")
                if action == "accept":
                    if not row["character_id"]:
                        raise ValueError("无法确认人物别名对应的角色")
                    conn.execute(
                        "INSERT OR IGNORE INTO character_aliases(id, work_id, character_id, alias, source, created_at) VALUES (?, ?, ?, ?, 'reviewed-extraction', ?)",
                        (str(uuid4()), work_id, row["character_id"], row["alias"], now_iso()),
                    )
                conn.execute(
                    "UPDATE character_alias_candidates SET status=?, reviewed_at=? WHERE id=?",
                    ("accepted" if action == "accept" else "rejected", now_iso(), item_id),
                )
            elif kind == "foreshadow":
                row = conn.execute("SELECT * FROM foreshadow_candidates WHERE id=? AND extraction_id=? LIMIT 1", (item_id, extraction_id)).fetchone()
                if not row:
                    raise ValueError("伏笔候选不存在")
                if row["status"] != "pending":
                    raise ValueError("伏笔候选已经审核过")
                edited = review.get("edited_value") if isinstance(review.get("edited_value"), dict) else {}
                if action == "accept":
                    conn.execute("""INSERT INTO foreshadows(id,work_id,clue,kind,planted_chapter,expected_reveal_chapter,
                        status,actual_reveal_chapter,note,evidence,created_at,updated_at) VALUES (?,?,?,?,?,?, 'open',0,'',?,?,?)""",
                                 (str(uuid4()), work_id, edited.get("clue", row["clue"]), edited.get("kind", row["kind"]),
                                  int(edited.get("planted_chapter", row["planted_chapter"])), int(edited.get("expected_reveal_chapter", row["expected_reveal_chapter"])),
                                  edited.get("evidence", row["evidence"]), now_iso(), now_iso()))
                conn.execute("UPDATE foreshadow_candidates SET status=?, reviewed_at=? WHERE id=?", ("accepted" if action == "accept" else "rejected", now_iso(), item_id))
            else:
                raise ValueError("未知的审核项目类型")

        pending = conn.execute(
            "SELECT COUNT(*) AS count FROM character_state_changes WHERE extraction_id=? AND status='pending'",
            (extraction_id,),
        ).fetchone()["count"]
        pending += conn.execute(
            "SELECT COUNT(*) AS count FROM timeline_events WHERE source_extraction_id=? AND review_status='pending'",
            (extraction_id,),
        ).fetchone()["count"]
        pending += conn.execute(
            "SELECT COUNT(*) AS count FROM character_alias_candidates WHERE extraction_id=? AND status='pending'",
            (extraction_id,),
        ).fetchone()["count"]
        pending += conn.execute(
            "SELECT COUNT(*) AS count FROM foreshadow_candidates WHERE extraction_id=? AND status='pending'",
            (extraction_id,),
        ).fetchone()["count"]
        if pending == 0:
            accepted = conn.execute(
                "SELECT COUNT(*) AS count FROM character_state_changes WHERE extraction_id=? AND status='accepted'",
                (extraction_id,),
            ).fetchone()["count"]
            accepted += conn.execute(
                "SELECT COUNT(*) AS count FROM timeline_events WHERE source_extraction_id=? AND review_status='confirmed'",
                (extraction_id,),
            ).fetchone()["count"]
            accepted += conn.execute(
                "SELECT COUNT(*) AS count FROM character_alias_candidates WHERE extraction_id=? AND status='accepted'",
                (extraction_id,),
            ).fetchone()["count"]
            accepted += conn.execute(
                "SELECT COUNT(*) AS count FROM foreshadow_candidates WHERE extraction_id=? AND status='accepted'",
                (extraction_id,),
            ).fetchone()["count"]
            conn.execute(
                "UPDATE state_extractions SET status=? WHERE id=?",
                ("applied" if accepted else "rejected", extraction_id),
            )
        return _format_extraction(conn, extraction_id)


def list_extractions(work_id: str, status: str | None = None) -> list[dict[str, Any]]:
    with transaction() as conn:
        if status:
            rows = conn.execute(
                "SELECT id FROM state_extractions WHERE work_id=? AND status=? ORDER BY created_at DESC",
                (work_id, status),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id FROM state_extractions WHERE work_id=? ORDER BY created_at DESC",
                (work_id,),
            ).fetchall()
        return [_format_extraction(conn, row["id"]) for row in rows]


def get_extraction(work_id: str, extraction_id: str) -> dict[str, Any] | None:
    with transaction() as conn:
        row = conn.execute(
            "SELECT id FROM state_extractions WHERE id=? AND work_id=? LIMIT 1",
            (extraction_id, work_id),
        ).fetchone()
        return _format_extraction(conn, extraction_id) if row else None
