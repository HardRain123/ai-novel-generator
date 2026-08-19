from typing import Any
from uuid import uuid4

from app.db import transaction
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


def _build_work(conn, work: dict[str, Any]) -> dict[str, Any]:
    work_id = work["id"]
    bible = conn.execute(
        "SELECT * FROM story_bibles WHERE work_id = ?", (work_id,)
    ).fetchone()
    characters = conn.execute(
        "SELECT * FROM characters WHERE work_id = ? ORDER BY created_at", (work_id,)
    ).fetchall()
    arcs = conn.execute(
        "SELECT * FROM plot_arcs WHERE work_id = ? ORDER BY sequence", (work_id,)
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

    result = dict(work)
    result["story_bible"] = _row(bible)
    result["characters"] = [_row(row) for row in characters]
    result["plot_arcs"] = [_row(row) for row in arcs]
    result["chapter_plans"] = [
        {
            **_row(row),
            "beats": json_loads(row["beats"], []),
            "opening_state": json_loads(row["opening_state_json"], {}),
            "causal_beats": json_loads(row["causal_beats_json"], []),
            "knowledge_changes": json_loads(row["knowledge_changes_json"], []),
            "state_changes": json_loads(row["state_changes_json"], []),
            "foreshadow_actions": json_loads(row["foreshadow_actions_json"], []),
            "forbidden_reveals": json_loads(row["forbidden_reveals_json"], []),
            "ending_state": json_loads(row["ending_state_json"], {}),
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
    return result


def create_work(payload: dict[str, Any], user_id: str = "demo-user") -> dict[str, Any]:
    work_id = str(uuid4())
    now = now_iso()
    with transaction() as conn:
        conn.execute(
            """
            INSERT INTO works(id, user_id, title, genre, target_audience,
                estimated_words, writing_style, premise, status, created_at, updated_at, model_profile_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?, ?)
            """,
            (
                work_id,
                user_id,
                payload["title"].strip(),
                payload.get("genre", "").strip(),
                payload.get("target_audience", "").strip(),
                payload.get("estimated_words", 100000),
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
        return _build_work(
            conn,
            dict(conn.execute("SELECT * FROM works WHERE id = ?", (work_id,)).fetchone()),
        )


def update_work(work_id: str, payload: dict[str, Any], user_id: str = "demo-user"):
    allowed = {
        key: value
        for key, value in payload.items()
        if (value is not None or key == "model_profile_id")
        and key in {"title", "genre", "target_audience", "estimated_words", "writing_style", "premise", "model_profile_id"}
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
        INSERT INTO story_bibles(id, work_id, summary, theme, world, ending, style_rules, locked, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(work_id) DO UPDATE SET
            summary=excluded.summary, theme=excluded.theme, world=excluded.world,
            ending=excluded.ending, style_rules=excluded.style_rules, updated_at=excluded.updated_at
        """,
        (
            str(uuid4()), work_id, bible.get("summary", ""), bible.get("theme", ""),
            bible.get("world", ""), bible.get("ending", ""), bible.get("style_rules", ""),
            int(bool(bible.get("locked", False))), now,
        ),
    )
    for item in data.get("characters", []):
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
            item.get("knowledge", ""), now,
        )
        if existing:
            conn.execute(
                """
                UPDATE characters SET role=?, goal=?, conflict=?, personality=?, background=?,
                    status=?, knowledge=?, updated_at=? WHERE id=?
                """,
                (*values, existing["id"]),
            )
        else:
            conn.execute(
                """
                INSERT INTO characters(id, work_id, name, role, goal, conflict, personality,
                    background, status, knowledge, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (str(uuid4()), work_id, name, *values[:-1], now, now),
            )
    for index, item in enumerate(data.get("plot_arcs", []), start=1):
        title = item.get("title", f"第{index}卷")
        existing = conn.execute(
            "SELECT id FROM plot_arcs WHERE work_id = ? AND title = ? LIMIT 1",
            (work_id, title),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE plot_arcs SET synopsis=?, sequence=?, status=? WHERE id=?",
                (item.get("synopsis", ""), item.get("sequence", index), item.get("status", "planned"), existing["id"]),
            )
        else:
            conn.execute(
                "INSERT INTO plot_arcs(id, work_id, title, synopsis, sequence, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (str(uuid4()), work_id, title, item.get("synopsis", ""), item.get("sequence", index), item.get("status", "planned"), now),
            )


def save_outline(conn, work_id: str, items: list[dict[str, Any]]) -> None:
    now = now_iso()
    for item in items:
        chapter_no = int(item.get("chapter_no", 1))
        conn.execute(
            """
            INSERT INTO chapter_plans(
                id, work_id, chapter_no, title, goal, conflict, beats, hook,
                pov_character, opening_state_json, causal_beats_json,
                knowledge_changes_json, state_changes_json, foreshadow_actions_json,
                forbidden_reveals_json, ending_state_json, status, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'planned', ?, ?)
            ON CONFLICT(work_id, chapter_no) DO UPDATE SET title=excluded.title, goal=excluded.goal,
                conflict=excluded.conflict, beats=excluded.beats, hook=excluded.hook,
                pov_character=excluded.pov_character, opening_state_json=excluded.opening_state_json,
                causal_beats_json=excluded.causal_beats_json, knowledge_changes_json=excluded.knowledge_changes_json,
                state_changes_json=excluded.state_changes_json, foreshadow_actions_json=excluded.foreshadow_actions_json,
                forbidden_reveals_json=excluded.forbidden_reveals_json, ending_state_json=excluded.ending_state_json,
                updated_at=excluded.updated_at
            """,
            (
                str(uuid4()), work_id, chapter_no, item.get("title", f"第{chapter_no}章"),
                item.get("goal", ""), item.get("conflict", ""), json_dumps(item.get("beats", [])),
                item.get("hook", ""), item.get("pov_character", ""),
                json_dumps(item.get("opening_state", {})), json_dumps(item.get("causal_beats", [])),
                json_dumps(item.get("knowledge_changes", [])), json_dumps(item.get("state_changes", [])),
                json_dumps(item.get("foreshadow_actions", [])), json_dumps(item.get("forbidden_reveals", [])),
                json_dumps(item.get("ending_state", {})), now, now,
            ),
        )


def save_chapter(conn, work_id: str, data: dict[str, Any]) -> None:
    chapter_no = int(data.get("chapter_no", 1))
    now = now_iso()
    conn.execute(
        """
        INSERT INTO chapters(id, work_id, chapter_no, title, content, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(work_id, chapter_no) DO UPDATE SET title=excluded.title,
            content=excluded.content, status=excluded.status, updated_at=excluded.updated_at
        """,
        (
            str(uuid4()), work_id, chapter_no, data.get("title", f"第{chapter_no}章"),
            data.get("content", ""), data.get("status", "draft"), now, now,
        ),
    )


def save_quality_report(conn, work_id: str, chapter_no: int, issues: list[dict[str, Any]], score: int):
    conn.execute(
        "INSERT INTO quality_reports(id, work_id, chapter_no, issues_json, score, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (str(uuid4()), work_id, chapter_no, json_dumps(issues), score, now_iso()),
    )
