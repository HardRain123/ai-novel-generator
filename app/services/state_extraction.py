import hashlib
import json
from typing import Any
from uuid import uuid4

from app.config import LLM_API_KEY, LLM_MODEL
from app.db import transaction
from app.services.novel_engine import STATE_EXTRACTOR_VERSION, engine
from app.utils import json_dumps, json_loads, now_iso


def _json_value(value: Any) -> str | None:
    return None if value is None else json_dumps(value)


def _resolve_character(conn, work_id: str, name: str):
    row = conn.execute(
        "SELECT id, name FROM characters WHERE work_id = ? AND name = ? LIMIT 1",
        (work_id, name),
    ).fetchone()
    if row:
        return row["id"], row["name"]
    row = conn.execute(
        """
        SELECT c.id, c.name FROM character_aliases a
        JOIN characters c ON c.id = a.character_id
        WHERE a.work_id = ? AND a.alias = ? LIMIT 1
        """,
        (work_id, name),
    ).fetchone()
    return (row["id"], row["name"]) if row else (None, name)


def _chapter_version(conn, work_id: str, chapter: dict[str, Any], source: str) -> dict[str, Any]:
    content = str(chapter.get("content", ""))
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    existing = conn.execute(
        "SELECT * FROM chapter_versions WHERE work_id = ? AND chapter_no = ? AND content_hash = ? LIMIT 1",
        (work_id, int(chapter["chapter_no"]), content_hash),
    ).fetchone()
    if existing:
        return dict(existing)
    latest = conn.execute(
        "SELECT COALESCE(MAX(version_no), 0) AS version_no FROM chapter_versions WHERE work_id = ? AND chapter_no = ?",
        (work_id, int(chapter["chapter_no"])),
    ).fetchone()
    chapter_id = str(chapter.get("id") or "")
    version = {
        "id": str(uuid4()),
        "work_id": work_id,
        "chapter_id": chapter_id,
        "chapter_no": int(chapter["chapter_no"]),
        "version_no": int(latest["version_no"] or 0) + 1,
        "content": content,
        "content_hash": content_hash,
        "source": source,
        "created_at": now_iso(),
    }
    conn.execute(
        """
        INSERT INTO chapter_versions(id, work_id, chapter_id, chapter_no, version_no, content, content_hash, source, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        tuple(version.values()),
    )
    return version


def _format_extraction(conn, extraction_id: str) -> dict[str, Any]:
    extraction = conn.execute(
        "SELECT * FROM state_extractions WHERE id = ? LIMIT 1", (extraction_id,)
    ).fetchone()
    if not extraction:
        return {}
    extraction_data = dict(extraction)
    raw = json_loads(extraction["raw_json"], {})
    extraction_data["raw"] = raw
    extraction_data["characters"] = []
    rows = conn.execute(
        "SELECT * FROM character_state_changes WHERE extraction_id = ? ORDER BY created_at, id",
        (extraction_id,),
    ).fetchall()
    for row in rows:
        item = dict(row)
        item["old_value"] = json_loads(item.pop("old_value_json"), None)
        item["new_value"] = json_loads(item.pop("new_value_json"), None)
        extraction_data["characters"].append(item)
    event_rows = conn.execute(
        "SELECT * FROM timeline_events WHERE source_extraction_id = ? ORDER BY event_order, id",
        (extraction_id,),
    ).fetchall()
    extraction_data["timeline_events"] = []
    for row in event_rows:
        item = dict(row)
        item["participants"] = json_loads(item.pop("participants_json"), [])
        extraction_data["timeline_events"].append(item)
    return extraction_data


def extract_and_persist(work: dict[str, Any], chapter: dict[str, Any], source: str = "generation") -> dict[str, Any]:
    """提取一章的状态变化并保存为 pending；绝不写入 character_states。"""
    result = engine.extract_state_changes(work, chapter)
    model_name = LLM_MODEL if LLM_API_KEY else "fallback"
    with transaction() as conn:
        version = _chapter_version(conn, work["id"], chapter, source)
        existing = conn.execute(
            "SELECT id FROM state_extractions WHERE chapter_version_id = ? AND extractor_version = ? LIMIT 1",
            (version["id"], STATE_EXTRACTOR_VERSION),
        ).fetchone()
        if existing:
            return _format_extraction(conn, existing["id"])

        extraction_id = str(uuid4())
        conn.execute(
            """
            INSERT INTO state_extractions(id, work_id, chapter_version_id, status, extractor_version, model, raw_json, warning, created_at)
            VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?)
            """,
            (
                extraction_id, work["id"], version["id"], STATE_EXTRACTOR_VERSION, model_name,
                json_dumps(result), "；".join(result.get("warnings", [])), now_iso(),
            ),
        )

        for character in result.get("characters", []):
            character_id, canonical_name = _resolve_character(conn, work["id"], character["character_name"])
            for alias in character.get("aliases", []):
                if character_id and alias != canonical_name:
                    conn.execute(
                        "INSERT OR IGNORE INTO character_aliases(id, work_id, character_id, alias, source, created_at) VALUES (?, ?, ?, ?, 'extraction', ?)",
                        (str(uuid4()), work["id"], character_id, alias, now_iso()),
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
                        str(uuid4()), work["id"], extraction_id, character_id, character.get("character_name", canonical_name),
                        change["field"], _json_value(change.get("old_value")), _json_value(change.get("new_value")),
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
                    str(uuid4()), work["id"], int(chapter["chapter_no"]) * 1000 + index, int(chapter["chapter_no"]),
                    event["title"], event.get("description", ""), now_iso(), event.get("story_time_text", ""),
                    event.get("time_type", "unknown"), event.get("location", ""), json_dumps(event.get("participants", [])),
                    event.get("evidence", ""), event.get("confidence", 0.5), extraction_id,
                ),
            )
        return _format_extraction(conn, extraction_id)


def list_extractions(work_id: str, status: str | None = None) -> list[dict[str, Any]]:
    with transaction() as conn:
        if status:
            rows = conn.execute(
                "SELECT id FROM state_extractions WHERE work_id = ? AND status = ? ORDER BY created_at DESC",
                (work_id, status),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id FROM state_extractions WHERE work_id = ? ORDER BY created_at DESC",
                (work_id,),
            ).fetchall()
        return [_format_extraction(conn, row["id"]) for row in rows]


def get_extraction(work_id: str, extraction_id: str) -> dict[str, Any] | None:
    with transaction() as conn:
        row = conn.execute(
            "SELECT id FROM state_extractions WHERE id = ? AND work_id = ? LIMIT 1",
            (extraction_id, work_id),
        ).fetchone()
        return _format_extraction(conn, extraction_id) if row else None

