import json
from typing import Any
from uuid import uuid4

from app.db import transaction
from app.services.character_cards import compact_character
from app.services.narrative_structure import append_continuation_volume, bootstrap_narrative_structure, suggested_target_chapters
from app.utils import json_dumps, json_loads, now_iso


def _row(row) -> dict[str, Any] | None:
    return dict(row) if row else None


def list_works(user_id: str = "demo-user") -> list[dict[str, Any]]:
    with transaction() as conn:
        rows = conn.execute(
            "SELECT * FROM works WHERE user_id = ? ORDER BY updated_at DESC", (user_id,)
        ).fetchall()
        return [_row(row) for row in rows]


def get_work(work_id: str, user_id: str = "demo-user") -> dict[str, Any] | None:
    with transaction() as conn:
        work = conn.execute(
            "SELECT * FROM works WHERE id = ? AND user_id = ?", (work_id, user_id)
        ).fetchone()
        if not work:
            return None
        return _build_work(conn, dict(work))


def ensure_long_form_structure(work_id: str, target_chapter_count: int | None = None) -> None:
    """Backfill editable volume/stage coordinates for pre-migration works.

    An untouched, automatically-created structure can be safely rebuilt when a
    user changes the book target before any chapter outline exists.  Once
    chapter plans exist, coordinates are treated as authored data and are not
    overwritten implicitly.
    """
    with transaction() as conn:
        work = conn.execute(
            "SELECT estimated_words, average_chapter_words, target_chapter_count FROM works WHERE id=?", (work_id,)
        ).fetchone()
        if not work:
            return
        target = int(target_chapter_count or work["target_chapter_count"] or suggested_target_chapters(work["estimated_words"], work["average_chapter_words"]))
        arcs = [dict(row) for row in conn.execute(
            "SELECT title, synopsis, sequence FROM plot_arcs WHERE work_id=? AND active=1 ORDER BY sequence", (work_id,)
        ).fetchall()]
        existing_end = conn.execute(
            "SELECT MAX(end_chapter) AS end_chapter FROM story_volumes WHERE work_id=?", (work_id,)
        ).fetchone()["end_chapter"]
        outlined = conn.execute(
            "SELECT 1 FROM chapter_plans WHERE work_id=? LIMIT 1", (work_id,)
        ).fetchone()
        # Volume coordinates are authored structure, while a chapter request can
        # be only a short generation window.  Never let a stale short target
        # shrink an existing 1—40 (or longer) volume before any chapters exist.
        if existing_end:
            target = max(target, int(existing_end))
        conn.execute("UPDATE works SET target_chapter_count=? WHERE id=?", (target, work_id))
        bootstrap_narrative_structure(
            conn,
            work_id,
            target,
            arcs,
            replace=bool(existing_end and int(existing_end) != target and not outlined),
        )
        if outlined and existing_end and int(existing_end) < target:
            append_continuation_volume(conn, work_id, int(existing_end) + 1, target)


def _build_work(conn, work: dict[str, Any]) -> dict[str, Any]:
    work_id = work["id"]
    bible = conn.execute(
        "SELECT * FROM story_bibles WHERE work_id = ?", (work_id,)
    ).fetchone()
    characters = conn.execute(
        "SELECT * FROM characters WHERE work_id = ? AND active = 1 ORDER BY created_at", (work_id,)
    ).fetchall()
    arcs = conn.execute(
        "SELECT * FROM plot_arcs WHERE work_id = ? AND active = 1 ORDER BY sequence", (work_id,)
    ).fetchall()
    plans = conn.execute(
        "SELECT * FROM chapter_plans WHERE work_id = ? ORDER BY chapter_no", (work_id,)
    ).fetchall()
    chapters = conn.execute(
        "SELECT * FROM chapters WHERE work_id = ? ORDER BY chapter_no", (work_id,)
    ).fetchall()
    timeline = conn.execute(
        """
        SELECT e.*, se.status AS source_extraction_status
        FROM timeline_events e
        LEFT JOIN state_extractions se ON se.id = e.source_extraction_id
        WHERE e.work_id = ? ORDER BY e.event_order, e.id
        """,
        (work_id,),
    ).fetchall()
    foreshadows = conn.execute(
        "SELECT * FROM foreshadows WHERE work_id = ? ORDER BY planted_chapter, id", (work_id,)
    ).fetchall()
    volumes = conn.execute(
        "SELECT * FROM story_volumes WHERE work_id=? ORDER BY sequence", (work_id,)
    ).fetchall()
    stages = conn.execute(
        "SELECT * FROM narrative_stages WHERE work_id=? ORDER BY start_chapter, sequence", (work_id,)
    ).fetchall()
    character_states = conn.execute(
        """
        SELECT s.*, c.name AS character_name, se.status AS source_extraction_status
        FROM character_states s
        JOIN characters c ON c.id = s.character_id
        LEFT JOIN chapter_versions cv ON cv.id = s.source_version_id
        LEFT JOIN state_extractions se ON se.chapter_version_id = cv.id AND se.status IN ('applied', 'superseded')
        WHERE s.work_id = ? ORDER BY c.created_at
        """,
        (work_id,),
    ).fetchall()
    character_state_history = conn.execute(
        """
        SELECT csc.*, cv.chapter_no, cv.id AS chapter_version_id,
               se.status AS source_extraction_status
        FROM character_state_changes csc
        JOIN state_extractions se ON se.id = csc.extraction_id
        JOIN chapter_versions cv ON cv.id = se.chapter_version_id
        WHERE csc.work_id = ?
          AND csc.status = 'accepted'
          AND se.status = 'applied'
        ORDER BY cv.chapter_no, csc.created_at, csc.id
        """,
        (work_id,),
    ).fetchall()
    reports = conn.execute(
        "SELECT * FROM quality_reports WHERE work_id = ? ORDER BY created_at DESC", (work_id,)
    ).fetchall()
    phases = conn.execute(
        "SELECT * FROM story_phases WHERE work_id=? ORDER BY start_day, phase_key", (work_id,)
    ).fetchall()
    factions = conn.execute(
        "SELECT * FROM factions WHERE work_id=? ORDER BY formed_day, name", (work_id,)
    ).fetchall()
    goals = conn.execute(
        "SELECT * FROM story_goals WHERE work_id=? ORDER BY priority DESC, created_at", (work_id,)
    ).fetchall()
    events = conn.execute(
        "SELECT * FROM story_events WHERE work_id=? AND status='confirmed' ORDER BY chapter_no, created_at", (work_id,)
    ).fetchall()
    outline_versions = conn.execute(
        "SELECT * FROM outline_versions WHERE work_id=? ORDER BY version_no DESC", (work_id,)
    ).fetchall()
    long_term_facts = conn.execute(
        "SELECT * FROM long_term_facts WHERE work_id=? ORDER BY entity_type, fact_key", (work_id,)
    ).fetchall()
    future_plans = conn.execute(
        "SELECT * FROM future_plans WHERE work_id=? AND status='active' ORDER BY target_chapter, created_at", (work_id,)
    ).fetchall()
    inspiration_blueprint = conn.execute(
        """
        SELECT b.id, b.analysis_id, b.content_json, b.originality_json, b.created_at
        FROM work_inspiration_blueprints wb
        JOIN inspiration_blueprints b ON b.id=wb.blueprint_id
        WHERE wb.work_id=?
        """,
        (work_id,),
    ).fetchone()
    inspiration_sources = conn.execute(
        "SELECT source, title, source_url, analysis_id, created_at FROM work_inspirations WHERE work_id=? ORDER BY created_at, title",
        (work_id,),
    ).fetchall()

    result = dict(work)
    result["story_bible"] = _row(bible)
    if result["story_bible"]:
        result["story_bible"]["must_have_elements"] = json_loads(
            result["story_bible"].get("must_have_elements_json"), []
        )
        result["story_bible"]["avoid_drift"] = json_loads(
            result["story_bible"].get("avoid_drift_json"), []
        )
        result["story_bible"]["quality_issues"] = json_loads(
            result["story_bible"].get("quality_issues_json"), []
        )
    result["characters"] = [compact_character({
        **_row(row),
        "facets": json_loads(row["facets_json"], {}),
    }) for row in characters]
    result["plot_arcs"] = [_row(row) for row in arcs]
    result["story_volumes"] = [{**_row(row), "ending_state": json_loads(row["ending_state_json"], {})} for row in volumes]
    result["narrative_stages"] = [{
        **_row(row),
        "entry_state": json_loads(row["entry_state_json"], {}),
        "exit_state": json_loads(row["exit_state_json"], {}),
        "allowed_payoffs": json_loads(row["allowed_payoffs_json"], []),
        "forbidden_payoffs": json_loads(row["forbidden_payoffs_json"], []),
        "prerequisites": json_loads(row["prerequisites_json"], []),
    } for row in stages]
    result["chapter_plans"] = [
        {
            **_row(row),
            "timeline_phase_key": row["phase_key"],
            "beats": json_loads(row["beats"], []),
            "opening_state": json_loads(row["opening_state_json"], {}),
            "causal_beats": json_loads(row["causal_beats_json"], []),
            "knowledge_changes": json_loads(row["knowledge_changes_json"], []),
            "state_changes": json_loads(row["state_changes_json"], []),
            "foreshadow_actions": json_loads(row["foreshadow_actions_json"], []),
            "forbidden_reveals": json_loads(row["forbidden_reveals_json"], []),
            "ending_state": json_loads(row["ending_state_json"], {}),
            "appearing_characters": json_loads(row["appearing_characters_json"], []),
            "appearing_factions": json_loads(row["appearing_factions_json"], []),
            "task_progress": json_loads(row["task_progress_json"], []),
            "dependencies": json_loads(row["dependencies_json"], []),
        }
        for row in plans
    ]
    result["chapters"] = [_row(row) for row in chapters]
    result["timeline_events"] = [_row(row) for row in timeline]
    result["foreshadows"] = [_row(row) for row in foreshadows]
    result["character_states"] = [
        {**_row(row), "state": json_loads(row["state_json"], {})} for row in character_states
    ]
    result["character_state_history"] = []
    for row in character_state_history:
        item = _row(row)
        item["old_value"] = json_loads(item.get("old_value_json"), None)
        item["new_value"] = json_loads(item.get("new_value_json"), None)
        item["reviewed_value"] = json_loads(item.get("reviewed_value_json"), None)
        result["character_state_history"].append(item)
    result["quality_reports"] = [
        {**_row(row), "issues": json_loads(row["issues_json"], [])} for row in reports
    ]
    result["story_phases"] = [{
        **_row(row), "rules": json_loads(row["rules_json"], []),
        "allowed": json_loads(row["allowed_json"], []), "forbidden": json_loads(row["forbidden_json"], []),
        "transition_conditions": json_loads(row["transition_conditions_json"], []),
    } for row in phases]
    result["factions"] = [{**_row(row), "state": json_loads(row["state_json"], {})} for row in factions]
    result["goals"] = [{
        **_row(row), "details": json_loads(row["details_json"], {}),
        "progress": json_loads(row["progress_json"], {}),
    } for row in goals]
    result["story_events"] = [
        {**_row(row), "before": json_loads(row["before_json"], {}), "after": json_loads(row["after_json"], {})}
        for row in events
    ]
    result["outline_versions"] = [{**_row(row), "request": json_loads(row["request_json"], {})} for row in outline_versions]
    result["long_term_facts"] = [{**_row(row), "value": json_loads(row["value_json"], {})} for row in long_term_facts]
    result["future_plans"] = [{**_row(row), "content": json_loads(row["content_json"], {})} for row in future_plans]
    result["inspiration_sources"] = [_row(row) for row in inspiration_sources]
    result["inspiration_blueprint"] = None
    if inspiration_blueprint:
        result["inspiration_blueprint"] = {
            **_row(inspiration_blueprint),
            "content": json_loads(inspiration_blueprint["content_json"], {}),
            "originality": json_loads(inspiration_blueprint["originality_json"], {}),
        }
    return result


def create_work(payload: dict[str, Any], user_id: str = "demo-user") -> dict[str, Any]:
    work_id = str(uuid4())
    now = now_iso()
    with transaction() as conn:
        conn.execute(
            """
            INSERT INTO works(id, user_id, title, genre, target_audience,
                estimated_words, average_chapter_words, target_chapter_count, writing_style, premise, status, created_at, updated_at, model_profile_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?, ?)
            """,
            (
                work_id,
                user_id,
                payload["title"].strip(),
                payload.get("genre", "").strip(),
                payload.get("target_audience", "").strip(),
                payload.get("estimated_words", 100000),
                payload.get("average_chapter_words", 2500),
                payload.get("target_chapter_count") or suggested_target_chapters(payload.get("estimated_words", 100000), payload.get("average_chapter_words", 2500)),
                payload.get("writing_style", "").strip(),
                payload.get("premise", "").strip(),
                now,
                now,
                payload.get("model_profile_id"),
            ),
        )
        conn.execute(
            "INSERT INTO story_bibles(id, work_id, updated_at) VALUES (?, ?, ?)",
            (str(uuid4()), work_id, now),
        )
        conn.execute(
            """
            INSERT INTO planning_sessions(id, work_id, status, current_step, preset, created_at, updated_at)
            VALUES (?, ?, 'in_progress', 'contract', 'custom', ?, ?)
            """,
            (str(uuid4()), work_id, now, now),
        )
        return _build_work(
            conn,
            dict(conn.execute("SELECT * FROM works WHERE id = ?", (work_id,)).fetchone()),
        )


def update_work(work_id: str, payload: dict[str, Any], user_id: str = "demo-user"):
    allowed = {
        key: value
        for key, value in payload.items()
        if (value is not None or key == "model_profile_id")
        and key in {"title", "genre", "target_audience", "estimated_words", "average_chapter_words", "target_chapter_count", "writing_style", "premise", "model_profile_id"}
    }
    if not allowed:
        return get_work(work_id, user_id)
    assignments = ", ".join(f"{key} = ?" for key in allowed)
    values = list(allowed.values()) + [now_iso(), work_id, user_id]
    with transaction() as conn:
        cur = conn.execute(
            f"UPDATE works SET {assignments}, updated_at = ? WHERE id = ? AND user_id = ?",
            values,
        )
        if cur.rowcount == 0:
            return None
    return get_work(work_id, user_id)


def update_character_card(conn, work_id: str, character_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Persist an author edit and mark only dependent plans/prose for review."""
    existing = conn.execute(
        "SELECT * FROM characters WHERE id=? AND work_id=? AND active=1", (character_id, work_id)
    ).fetchone()
    if not existing:
        raise ValueError("人物不存在或已归档")
    old_name = str(existing["name"] or "").strip()
    previous = compact_character({**dict(existing), "facets": json_loads(existing["facets_json"], {})})
    merged = {**previous, **payload}
    if isinstance(payload.get("dramatic_core"), dict):
        merged["dramatic_core"] = {**previous.get("dramatic_core", {}), **payload["dramatic_core"]}
    item = compact_character(merged)
    name = str(item.get("name") or "").strip()
    if not name:
        raise ValueError("人物姓名不能为空")
    duplicate = conn.execute(
        "SELECT id FROM characters WHERE work_id=? AND active=1 AND name=? AND id<>? LIMIT 1",
        (work_id, name, character_id),
    ).fetchone()
    if duplicate:
        raise ValueError("同一作品中不能有重名人物")

    now = now_iso()
    conn.execute(
        """
        UPDATE characters SET name=?, role=?, goal=?, conflict=?, personality=?, background=?,
            status=?, knowledge=?, biography=?, motivation=?, flaw=?, character_arc=?, secret=?,
            relationships=?, voice=?, story_function=?, appearance=?, portrayal=?, facets_json=?, updated_at=?
        WHERE id=?
        """,
        (
            name, item.get("role", ""), item.get("goal", ""), item.get("conflict", ""),
            item.get("personality", ""), item.get("background", ""), item.get("status", ""),
            item.get("knowledge", ""), item.get("biography", ""), item.get("motivation", ""),
            item.get("flaw", ""), item.get("character_arc", ""), item.get("secret", ""),
            item.get("relationships", ""), item.get("voice", ""), item.get("story_function", ""),
            item.get("appearance", ""), item.get("portrayal", ""), json_dumps(item.get("facets", {})), now,
            character_id,
        ),
    )

    affected_plans: list[int] = []
    plans = conn.execute(
        "SELECT chapter_no, pov_character, appearing_characters_json FROM chapter_plans WHERE work_id=?",
        (work_id,),
    ).fetchall()
    known_names = {old_name, name}
    for plan in plans:
        appearing = set(json_loads(plan["appearing_characters_json"], []))
        if str(plan["pov_character"] or "") in known_names or appearing.intersection(known_names):
            affected_plans.append(int(plan["chapter_no"]))
    if affected_plans:
        placeholders = ",".join("?" for _ in affected_plans)
        conn.execute(
            f"UPDATE chapter_plans SET stale_reason=CASE WHEN stale_reason='' THEN '人物卡已更新，请复核人物动机、关系与出场安排' ELSE stale_reason END, updated_at=? WHERE work_id=? AND chapter_no IN ({placeholders})",
            (now, work_id, *affected_plans),
        )
        conn.execute(
            f"UPDATE chapters SET stale_reason=CASE WHEN stale_reason='' THEN '人物卡已更新；正文不会自动改写，请复核人物呈现' ELSE stale_reason END, updated_at=? WHERE work_id=? AND chapter_no IN ({placeholders})",
            (now, work_id, *affected_plans),
        )
    conn.execute("UPDATE works SET updated_at=? WHERE id=?", (now, work_id))
    return {"affected_plan_count": len(affected_plans), "affected_chapter_count": conn.execute(
        "SELECT COUNT(*) FROM chapters WHERE work_id=? AND chapter_no IN (" + ",".join("?" for _ in affected_plans) + ")",
        (work_id, *affected_plans),
    ).fetchone()[0] if affected_plans else 0}


def delete_work(work_id: str, user_id: str = "demo-user") -> bool:
    """Delete a work and all of its cascaded story assets."""
    with transaction() as conn:
        active_job = conn.execute(
            "SELECT 1 FROM generation_jobs WHERE work_id=? AND status IN ('queued','running','cancel_requested') LIMIT 1",
            (work_id,),
        ).fetchone()
        if active_job:
            raise ValueError("作品正在生成任务中，请先等待任务完成或取消后再删除")
        return conn.execute(
            "DELETE FROM works WHERE id=? AND user_id=?",
            (work_id, user_id),
        ).rowcount > 0


def save_story_setup(conn, work_id: str, data: dict[str, Any]) -> None:
    now = now_iso()
    existing_bible = conn.execute(
        "SELECT locked FROM story_bibles WHERE work_id = ? LIMIT 1", (work_id,)
    ).fetchone()
    if existing_bible and int(existing_bible["locked"] or 0):
        return
    bible = data.get("story_bible") or {}
    conn.execute(
        """
        INSERT INTO story_bibles(
            id, work_id, summary, theme, world, ending, style_rules,
            title_interpretation, reader_promise, core_hook, core_conflict, stakes,
            must_have_elements_json, avoid_drift_json, generation_source,
            quality_score, quality_issues_json, locked, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(work_id) DO UPDATE SET
            summary=excluded.summary, theme=excluded.theme, world=excluded.world,
            ending=excluded.ending, style_rules=excluded.style_rules,
            title_interpretation=excluded.title_interpretation,
            reader_promise=excluded.reader_promise, core_hook=excluded.core_hook,
            core_conflict=excluded.core_conflict, stakes=excluded.stakes,
            must_have_elements_json=excluded.must_have_elements_json,
            avoid_drift_json=excluded.avoid_drift_json,
            generation_source=excluded.generation_source, quality_score=excluded.quality_score,
            quality_issues_json=excluded.quality_issues_json, updated_at=excluded.updated_at
        """,
        (
            str(uuid4()), work_id, bible.get("summary", ""), bible.get("theme", ""),
            bible.get("world", ""), bible.get("ending", ""), bible.get("style_rules", ""),
            bible.get("title_interpretation", ""), bible.get("reader_promise", ""),
            bible.get("core_hook", ""), bible.get("core_conflict", ""), bible.get("stakes", ""),
            json_dumps(bible.get("must_have_elements", [])), json_dumps(bible.get("avoid_drift", [])),
            data.get("generation_source", ""), int(data.get("quality_score", 0)),
            json_dumps(data.get("quality_issues", [])), int(bool(bible.get("locked", False))), now,
        ),
    )
    conn.execute("UPDATE characters SET active=0 WHERE work_id=?", (work_id,))
    for raw_item in data.get("characters", []):
        item = compact_character(raw_item)
        name = str(item.get("name", "")).strip()
        if not name:
            continue
        existing = conn.execute(
            "SELECT id FROM characters WHERE work_id = ? AND name = ? LIMIT 1",
            (work_id, name),
        ).fetchone()
        values = (
            item.get("role", ""), item.get("goal", ""), item.get("conflict", ""),
            item.get("personality", ""), item.get("background", ""), item.get("status", ""),
            item.get("knowledge", ""), item.get("biography", ""), item.get("motivation", ""),
            item.get("flaw", ""), item.get("character_arc", ""), item.get("secret", ""),
            item.get("relationships", ""), item.get("voice", ""), item.get("story_function", ""),
            item.get("appearance", ""), item.get("portrayal", ""), json_dumps(item.get("facets", {})), now,
        )
        if existing:
            conn.execute(
                """
                UPDATE characters SET role=?, goal=?, conflict=?, personality=?, background=?,
                    status=?, knowledge=?, biography=?, motivation=?, flaw=?, character_arc=?,
                    secret=?, relationships=?, voice=?, story_function=?, appearance=?, portrayal=?, facets_json=?, updated_at=?, active=1 WHERE id=?
                """,
                (*values, existing["id"]),
            )
        else:
            conn.execute(
                """
                INSERT INTO characters(id, work_id, name, role, goal, conflict, personality,
                    background, status, knowledge, biography, motivation, flaw, character_arc,
                    secret, relationships, voice, story_function, appearance, portrayal, facets_json, created_at, updated_at, active)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (str(uuid4()), work_id, name, *values[:-1], now, now),
            )
    conn.execute("UPDATE plot_arcs SET active=0 WHERE work_id=?", (work_id,))
    for index, item in enumerate(data.get("plot_arcs", []), start=1):
        title = item.get("title", f"第{index}卷")
        existing = conn.execute(
            "SELECT id FROM plot_arcs WHERE work_id = ? AND title = ? LIMIT 1",
            (work_id, title),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE plot_arcs SET synopsis=?, sequence=?, status=?, active=1 WHERE id=?",
                (item.get("synopsis", ""), item.get("sequence", index), item.get("status", "planned"), existing["id"]),
            )
        else:
            conn.execute(
                "INSERT INTO plot_arcs(id, work_id, title, synopsis, sequence, status, created_at, active) VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
                (str(uuid4()), work_id, title, item.get("synopsis", ""), item.get("sequence", index), item.get("status", "planned"), now),
            )
    target = conn.execute(
        "SELECT estimated_words, average_chapter_words, target_chapter_count FROM works WHERE id=?", (work_id,)
    ).fetchone()
    target_chapters = int(target["target_chapter_count"] or suggested_target_chapters(target["estimated_words"], target["average_chapter_words"]))
    conn.execute("UPDATE works SET target_chapter_count=?, updated_at=? WHERE id=?", (target_chapters, now, work_id))
    bootstrap_narrative_structure(conn, work_id, target_chapters, data.get("plot_arcs", []), replace=True)


def _plan_version_payload(row) -> dict[str, Any]:
    """Serialize every plan field needed by history and Markdown export."""
    return {
        "chapter_no": row["chapter_no"], "title": row["title"], "goal": row["goal"],
        "conflict": row["conflict"], "beats": json_loads(row["beats"], []), "hook": row["hook"],
        "failure_cost": row["failure_cost"],
        "pov_character": row["pov_character"], "opening_state": json_loads(row["opening_state_json"], {}),
        "causal_beats": json_loads(row["causal_beats_json"], []),
        "knowledge_changes": json_loads(row["knowledge_changes_json"], []),
        "state_changes": json_loads(row["state_changes_json"], []),
        "foreshadow_actions": json_loads(row["foreshadow_actions_json"], []),
        "forbidden_reveals": json_loads(row["forbidden_reveals_json"], []),
        "ending_state": json_loads(row["ending_state_json"], {}), "plot_arc": row["plot_arc"],
        "title_promise_progress": row["title_promise_progress"],
        "character_arc_progress": row["character_arc_progress"], "story_day": row["story_day"],
        "phase_key": row["phase_key"], "time_mode": row["time_mode"],
        "appearing_characters": json_loads(row["appearing_characters_json"], []),
        "appearing_factions": json_loads(row["appearing_factions_json"], []),
        "task_progress": json_loads(row["task_progress_json"], []),
        "start_time": row["start_time"], "end_time": row["end_time"],
        "previous_chapter_no": row["previous_chapter_no"], "fact_version": row["fact_version"],
        "dependencies": json_loads(row["dependencies_json"], []), "version": row["version"],
        "outline_version": row["outline_version"], "calibration_status": row["calibration_status"],
        "volume_id": row["volume_id"], "narrative_stage_id": row["narrative_stage_id"],
    }


def _create_outline_version(
    conn, work_id: str, *, mode: str, from_chapter: int, to_chapter: int,
    request: dict[str, Any] | None, expected_outline_version: int | None,
    expected_fact_version: int | None,
) -> tuple[str, int, int]:
    work = conn.execute("SELECT fact_version FROM works WHERE id=? LIMIT 1", (work_id,)).fetchone()
    if not work:
        raise ValueError("作品不存在")
    fact_version = int(work["fact_version"] or 0)
    if expected_fact_version is not None and int(expected_fact_version) != fact_version:
        raise ValueError("事实版本已变化，拒绝保存过期的大纲生成结果")
    latest = conn.execute(
        "SELECT COALESCE(MAX(version_no), 0) AS version_no FROM outline_versions WHERE work_id=?", (work_id,)
    ).fetchone()
    current_outline_version = int(latest["version_no"] or 0)
    if expected_outline_version is not None and int(expected_outline_version) != current_outline_version:
        raise ValueError("大纲版本已变化，请刷新后再保存")
    version_no = current_outline_version + 1
    version_id = str(uuid4())
    conn.execute("UPDATE outline_versions SET status='historical' WHERE work_id=? AND status='active'", (work_id,))
    conn.execute(
        """INSERT INTO outline_versions(id,work_id,version_no,mode,from_chapter,to_chapter,fact_version,status,request_json,created_at)
           VALUES (?,?,?,?,?,?,?,'active',?,?)""",
        (version_id, work_id, version_no, mode, int(from_chapter), int(to_chapter), fact_version,
         json_dumps(request or {}), now_iso()),
    )
    return version_id, version_no, fact_version


def save_outline(
    conn,
    work_id: str,
    items: list[dict[str, Any]],
    *,
    mode: str = "initial",
    from_chapter: int = 1,
    to_chapter: int | None = None,
    request: dict[str, Any] | None = None,
    expected_outline_version: int | None = None,
    expected_fact_version: int | None = None,
) -> dict[str, Any]:
    """Persist a complete/ranged outline without silently losing historical plans."""
    if mode not in {"initial", "replan", "extend", "manual"}:
        raise ValueError("不支持的大纲保存模式")
    chapter_numbers = sorted({int(item.get("chapter_no", 0)) for item in items if int(item.get("chapter_no", 0)) > 0})
    if not chapter_numbers:
        raise ValueError("大纲至少需要一章")
    if len(chapter_numbers) != len(items):
        raise ValueError("大纲章节编号不能为空且不得重复")
    if to_chapter is None:
        to_chapter = max(chapter_numbers)
    if mode == "initial" and chapter_numbers != list(range(1, int(to_chapter) + 1)):
        raise ValueError("首次生成的大纲必须从第1章连续编号到目标章数")
    if mode in {"replan", "extend"} and chapter_numbers != list(range(int(from_chapter), int(to_chapter) + 1)):
        raise ValueError("重新规划或扩展的大纲必须在请求范围内连续编号")

    outline_version_id, outline_version, fact_version = _create_outline_version(
        conn, work_id, mode=mode, from_chapter=from_chapter, to_chapter=int(to_chapter),
        request=request, expected_outline_version=expected_outline_version,
        expected_fact_version=expected_fact_version,
    )
    now = now_iso()
    if mode == "initial":
        conn.execute("UPDATE chapter_plan_versions SET status='superseded', superseded_by=? WHERE work_id=? AND status='active'", (outline_version_id, work_id))
        conn.execute("DELETE FROM chapter_plans WHERE work_id=? AND chapter_no NOT IN (%s)" % ",".join("?" for _ in chapter_numbers), (work_id, *chapter_numbers))
    elif mode == "replan":
        conn.execute(
            "UPDATE chapter_plan_versions SET status='superseded', superseded_by=? WHERE work_id=? AND chapter_no BETWEEN ? AND ? AND status='active'",
            (outline_version_id, work_id, int(from_chapter), int(to_chapter)),
        )

    for item in items:
        chapter_no = int(item["chapter_no"])
        conn.execute(
            """
            INSERT INTO chapter_plans(
                id, work_id, chapter_no, title, goal, conflict, beats, hook,
                pov_character, opening_state_json, causal_beats_json,
                knowledge_changes_json, state_changes_json, foreshadow_actions_json,
                forbidden_reveals_json, ending_state_json, plot_arc,
                title_promise_progress, character_arc_progress, story_day, phase_key, failure_cost,
                appearing_characters_json, appearing_factions_json, task_progress_json,
                time_mode, start_time, end_time, previous_chapter_no, fact_version, outline_version,
                calibration_status, version, dependencies_json, stale_reason, status, created_at, updated_at
            )
            VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12,?13,?14,?15,?16,?17,?18,?19,?20,?21,?22,?23,?24,?25,?26,?27,?28,?29,?30,?31,?32,1,?33,'','planned',?34,?35)
            ON CONFLICT(work_id, chapter_no) DO UPDATE SET title=excluded.title, goal=excluded.goal,
                conflict=excluded.conflict, beats=excluded.beats, hook=excluded.hook,
                pov_character=excluded.pov_character, opening_state_json=excluded.opening_state_json,
                causal_beats_json=excluded.causal_beats_json, knowledge_changes_json=excluded.knowledge_changes_json,
                state_changes_json=excluded.state_changes_json, foreshadow_actions_json=excluded.foreshadow_actions_json,
                forbidden_reveals_json=excluded.forbidden_reveals_json, ending_state_json=excluded.ending_state_json,
                plot_arc=excluded.plot_arc, title_promise_progress=excluded.title_promise_progress,
                character_arc_progress=excluded.character_arc_progress, story_day=excluded.story_day,
                phase_key=excluded.phase_key, failure_cost=excluded.failure_cost,
                appearing_characters_json=excluded.appearing_characters_json,
                appearing_factions_json=excluded.appearing_factions_json,
                task_progress_json=excluded.task_progress_json,
                time_mode=excluded.time_mode, start_time=excluded.start_time,
                end_time=excluded.end_time, previous_chapter_no=excluded.previous_chapter_no,
                fact_version=excluded.fact_version, outline_version=excluded.outline_version,
                calibration_status=excluded.calibration_status, dependencies_json=excluded.dependencies_json,
                stale_reason='', version=chapter_plans.version+1, updated_at=excluded.updated_at
            """,
            (
                str(uuid4()), work_id, chapter_no, item.get("title", f"第{chapter_no}章"), item.get("goal", ""),
                item.get("conflict", ""), json_dumps(item.get("beats", [])), item.get("hook", ""),
                item.get("pov_character", ""), json_dumps(item.get("opening_state", {})),
                json_dumps(item.get("causal_beats", [])), json_dumps(item.get("knowledge_changes", [])),
                json_dumps(item.get("state_changes", [])), json_dumps(item.get("foreshadow_actions", [])),
                json_dumps(item.get("forbidden_reveals", [])), json_dumps(item.get("ending_state", {})),
                item.get("plot_arc", ""), item.get("title_promise_progress", ""), item.get("character_arc_progress", ""),
                item.get("story_day"), item.get("phase_key", ""), item.get("failure_cost", ""),
                json_dumps(item.get("appearing_characters", [])), json_dumps(item.get("appearing_factions", [])),
                json_dumps(item.get("task_progress", [])), item.get("time_mode", "linear"),
                item.get("start_time", ""), item.get("end_time", ""), item.get("previous_chapter_no", chapter_no - 1 if chapter_no > 1 else None),
                fact_version, outline_version,
                item.get("calibration_status", "calibrated" if item.get("story_day") is not None and item.get("phase_key") else "pending_calibration"),
                json_dumps(item.get("dependencies", [])), now, now,
            ),
        )
        conn.execute(
            "UPDATE chapter_plans SET volume_id=?, narrative_stage_id=? WHERE work_id=? AND chapter_no=?",
            (item.get("volume_id"), item.get("narrative_stage_id"), work_id, chapter_no),
        )
        stored = conn.execute("SELECT * FROM chapter_plans WHERE work_id=? AND chapter_no=?", (work_id, chapter_no)).fetchone()
        conn.execute(
            "INSERT INTO chapter_plan_versions(id,work_id,chapter_no,outline_version_id,plan_version,content_json,status,created_at) VALUES (?,?,?,?,?,?,'active',?)",
            (str(uuid4()), work_id, chapter_no, outline_version_id, int(stored["version"]), json_dumps(_plan_version_payload(stored)), now),
        )
    conn.execute(
        """UPDATE chapters SET stale_reason='章节大纲已更新，请复核或重写正文'
           WHERE work_id=? AND chapter_no IN (
             SELECT p.chapter_no FROM chapter_plans p
             WHERE p.work_id=? AND chapters.source_plan_version > 0
               AND chapters.source_plan_version <> p.version
           )""",
        (work_id, work_id),
    )
    return {"outline_version": outline_version, "outline_version_id": outline_version_id, "fact_version": fact_version}


def save_chapter(conn, work_id: str, data: dict[str, Any]) -> None:
    chapter_no = int(data.get("chapter_no", 1))
    now = now_iso()
    plan = conn.execute(
        "SELECT version FROM chapter_plans WHERE work_id=? AND chapter_no=?", (work_id, chapter_no)
    ).fetchone()
    plan_version = int(plan["version"]) if plan else 0
    conn.execute(
        """
        INSERT INTO chapters(id, work_id, chapter_no, title, content, status, source_plan_version, stale_reason, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, '', ?, ?)
        ON CONFLICT(work_id, chapter_no) DO UPDATE SET title=excluded.title,
            content=excluded.content, status=excluded.status, source_plan_version=excluded.source_plan_version,
            stale_reason='', updated_at=excluded.updated_at
        """,
        (
            str(uuid4()), work_id, chapter_no, data.get("title", f"第{chapter_no}章"),
            data.get("content", ""), data.get("status", "draft"), plan_version, now, now,
        ),
    )


def update_chapter_plan(conn, work_id: str, chapter_no: int, data: dict[str, Any]) -> None:
    """Author edits are canonical and intentionally invalidate only dependent prose."""
    existing = conn.execute(
        "SELECT * FROM chapter_plans WHERE work_id=? AND chapter_no=?", (work_id, chapter_no)
    ).fetchone()
    if not existing:
        raise ValueError("章节大纲不存在")
    now = now_iso()
    outline_version_id, outline_version, fact_version = _create_outline_version(
        conn, work_id, mode="manual", from_chapter=chapter_no, to_chapter=chapter_no,
        request={"chapter_no": chapter_no, "source": "author_edit"},
        expected_outline_version=None, expected_fact_version=None,
    )
    conn.execute(
        "UPDATE chapter_plan_versions SET status='superseded', superseded_by=? WHERE work_id=? AND chapter_no=? AND status='active'",
        (outline_version_id, work_id, chapter_no),
    )
    fields = {
        "title": data.get("title", existing["title"]), "goal": data.get("goal", existing["goal"]),
        "conflict": data.get("conflict", existing["conflict"]), "beats": json_dumps(data.get("beats", json_loads(existing["beats"], []))),
        "hook": data.get("hook", existing["hook"]), "pov_character": data.get("pov_character", existing["pov_character"]),
        "opening_state_json": json_dumps(data.get("opening_state", json_loads(existing["opening_state_json"], {}))),
        "causal_beats_json": json_dumps(data.get("causal_beats", json_loads(existing["causal_beats_json"], []))),
        "knowledge_changes_json": json_dumps(data.get("knowledge_changes", json_loads(existing["knowledge_changes_json"], []))),
        "state_changes_json": json_dumps(data.get("state_changes", json_loads(existing["state_changes_json"], []))),
        "foreshadow_actions_json": json_dumps(data.get("foreshadow_actions", json_loads(existing["foreshadow_actions_json"], []))),
        "forbidden_reveals_json": json_dumps(data.get("forbidden_reveals", json_loads(existing["forbidden_reveals_json"], []))),
        "ending_state_json": json_dumps(data.get("ending_state", json_loads(existing["ending_state_json"], {}))),
        "plot_arc": data.get("plot_arc", existing["plot_arc"]),
        "title_promise_progress": data.get("title_promise_progress", existing["title_promise_progress"]),
        "character_arc_progress": data.get("character_arc_progress", existing["character_arc_progress"]),
        "story_day": data.get("story_day", existing["story_day"]), "phase_key": data.get("phase_key", existing["phase_key"]),
        "failure_cost": data.get("failure_cost", existing["failure_cost"]),
        "appearing_characters_json": json_dumps(data.get("appearing_characters", json_loads(existing["appearing_characters_json"], []))),
        "appearing_factions_json": json_dumps(data.get("appearing_factions", json_loads(existing["appearing_factions_json"], []))),
        "task_progress_json": json_dumps(data.get("task_progress", json_loads(existing["task_progress_json"], []))),
        "time_mode": data.get("time_mode", existing["time_mode"]),
        "start_time": data.get("start_time", existing["start_time"]),
        "end_time": data.get("end_time", existing["end_time"]),
        "previous_chapter_no": data.get("previous_chapter_no", existing["previous_chapter_no"]),
        "fact_version": fact_version, "outline_version": outline_version,
        "calibration_status": data.get("calibration_status", existing["calibration_status"]),
        "dependencies_json": json_dumps(data.get("dependencies", json_loads(existing["dependencies_json"], []))),
    }
    assignments = ", ".join(f"{key}=?" for key in fields)
    conn.execute(
        f"UPDATE chapter_plans SET {assignments}, version=version+1, stale_reason='', updated_at=? WHERE work_id=? AND chapter_no=?",
        (*fields.values(), now, work_id, chapter_no),
    )
    stored = conn.execute("SELECT * FROM chapter_plans WHERE work_id=? AND chapter_no=?", (work_id, chapter_no)).fetchone()
    conn.execute(
        "INSERT INTO chapter_plan_versions(id,work_id,chapter_no,outline_version_id,plan_version,content_json,status,created_at) VALUES (?,?,?,?,?,?,'active',?)",
        (str(uuid4()), work_id, chapter_no, outline_version_id, int(stored["version"]), json_dumps(_plan_version_payload(stored)), now),
    )
    conn.execute(
        "UPDATE chapters SET stale_reason='章节大纲已由作者修改，请复核或重写正文' WHERE work_id=? AND chapter_no=?",
        (work_id, chapter_no),
    )
    conn.execute(
        "UPDATE chapter_plans SET stale_reason='前置章节大纲已修改，请复核承接关系' WHERE work_id=? AND chapter_no>?",
        (work_id, chapter_no),
    )


def save_quality_report(conn, work_id: str, chapter_no: int, issues: list[dict[str, Any]], score: int):
    conn.execute(
        "INSERT INTO quality_reports(id, work_id, chapter_no, issues_json, score, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (str(uuid4()), work_id, chapter_no, json_dumps(issues), score, now_iso()),
    )


def list_outline_versions(work_id: str) -> list[dict[str, Any]]:
    with transaction() as conn:
        rows = conn.execute("SELECT * FROM outline_versions WHERE work_id=? ORDER BY version_no DESC", (work_id,)).fetchall()
        return [{**dict(row), "request": json_loads(row["request_json"], {})} for row in rows]


def list_chapter_plan_history(work_id: str, chapter_no: int) -> list[dict[str, Any]]:
    with transaction() as conn:
        rows = conn.execute(
            """SELECT cpv.*, ov.version_no AS outline_version, ov.mode AS outline_mode, ov.fact_version
               FROM chapter_plan_versions cpv JOIN outline_versions ov ON ov.id=cpv.outline_version_id
               WHERE cpv.work_id=? AND cpv.chapter_no=? ORDER BY cpv.created_at DESC, cpv.plan_version DESC""",
            (work_id, chapter_no),
        ).fetchall()
        return [{**dict(row), "content": json_loads(row["content_json"], {})} for row in rows]


def export_complete_outline(work_id: str) -> str:
    work = get_work(work_id)
    if not work:
        raise ValueError("作品不存在")
    lines = [f"# 《{work['title']}》完整章节大纲", "", f"- 当前事实版本：{work.get('fact_version', 0)}", f"- 当前大纲版本：{max((item.get('version_no', 0) for item in work.get('outline_versions', [])), default=0)}", ""]
    for plan in work.get("chapter_plans") or []:
        lines.extend([
            f"## 第{plan.get('chapter_no')}章 {plan.get('title', '')}", "",
            "```json", json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True), "```", "",
        ])
    return "\n".join(lines)
