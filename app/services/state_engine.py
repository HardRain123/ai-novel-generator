"""Canonical story facts: event replay, materialized snapshots and rollback.

The event log remains authoritative.  ``character_states`` and
``story_snapshots`` are deliberately treated as rebuildable read models.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from app.db import transaction
from app.utils import json_dumps, json_loads, now_iso


def _decode_event(row: Any) -> dict[str, Any]:
    item = dict(row)
    item["before"] = json_loads(item.pop("before_json", "{}"), {})
    item["after"] = json_loads(item.pop("after_json", "{}"), {})
    return item


def current_fact_version(conn, work_id: str) -> int:
    row = conn.execute("SELECT fact_version FROM works WHERE id=?", (work_id,)).fetchone()
    stored = int((row["fact_version"] if row else 0) or 0)
    event_row = conn.execute(
        "SELECT COALESCE(MAX(fact_version), 0) AS value FROM story_events WHERE work_id=?",
        (work_id,),
    ).fetchone()
    return max(stored, int((event_row["value"] if event_row else 0) or 0))


def _advance_fact_version(conn, work_id: str, source_event_id: str | None, reason: str) -> tuple[int, str]:
    version = current_fact_version(conn, work_id) + 1
    version_id = str(uuid4())
    now = now_iso()
    conn.execute(
        "INSERT INTO fact_versions(id,work_id,version_no,as_of_chapter,source_event_id,reason,created_at) VALUES (?,?,?,?,?,?,?)",
        (version_id, work_id, version, 0, source_event_id, reason, now),
    )
    conn.execute("UPDATE works SET fact_version=?, updated_at=? WHERE id=?", (version, now, work_id))
    return version, version_id


def record_event(
    conn,
    work_id: str,
    *,
    chapter_no: int,
    chapter_version_id: str | None,
    story_day: int | None,
    event_type: str,
    entity_type: str,
    entity_id: str | None,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    evidence: str = "",
    confidence: float = 1.0,
    risk_level: str = "low",
    status: str = "confirmed",
    reversal_of: str | None = None,
) -> dict[str, Any]:
    """Append a versioned event and atomically advance the fact version."""
    event_id = str(uuid4())
    version, _ = _advance_fact_version(conn, work_id, event_id, event_type)
    now = now_iso()
    conn.execute(
        """INSERT INTO story_events(
            id,work_id,chapter_no,chapter_version_id,story_day,event_type,entity_type,entity_id,
            before_json,after_json,evidence,status,created_at,fact_version,source_fact_version,
            confidence,risk_level,reversal_of,reviewed_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            event_id, work_id, int(chapter_no), chapter_version_id, story_day, event_type,
            entity_type, entity_id, json_dumps(before or {}), json_dumps(after or {}), evidence,
            status, now, version, version - 1, max(0.0, min(1.0, float(confidence))), risk_level,
            reversal_of, now if status == "confirmed" else None,
        ),
    )
    conn.execute(
        "UPDATE fact_versions SET as_of_chapter=? WHERE id=?",
        (int(chapter_no), _fact_version_id(conn, work_id, version)),
    )
    # A changed canonical fact must never leave downstream plans looking fresh.
    # Chapter-zero events are author-level facts and affect the entire outline;
    # chapter-bound events affect only the following chapter contracts.
    affected_from = 1 if int(chapter_no) <= 0 else int(chapter_no) + 1
    conn.execute(
        """UPDATE chapter_plans SET stale_reason=?, updated_at=?
           WHERE work_id=? AND chapter_no>=? AND fact_version<? AND stale_reason=''""",
        (f"事实版本已更新为 v{version}（{event_type}），请复核承接关系", now, work_id, affected_from, version),
    )
    return {"id": event_id, "fact_version": version, "source_fact_version": version - 1}


def _fact_version_id(conn, work_id: str, version: int) -> str:
    row = conn.execute(
        "SELECT id FROM fact_versions WHERE work_id=? AND version_no=? LIMIT 1", (work_id, version)
    ).fetchone()
    return str(row["id"]) if row else ""


def _event_rows(conn, work_id: str, chapter_limit: int | None, *, before: bool = False) -> list[dict[str, Any]]:
    where = ["work_id=?", "status='confirmed'"]
    params: list[Any] = [work_id]
    if chapter_limit is not None:
        where.append("chapter_no {} ?".format("<" if before else "<="))
        params.append(int(chapter_limit))
    rows = conn.execute(
        f"SELECT * FROM story_events WHERE {' AND '.join(where)} ORDER BY chapter_no, fact_version, created_at, id",
        params,
    ).fetchall()
    return [_decode_event(row) for row in rows]


def _apply_event(state: dict[str, Any], event: dict[str, Any]) -> None:
    event_type = str(event.get("event_type") or "")
    entity_type = str(event.get("entity_type") or "")
    entity_id = str(event.get("entity_id") or "")
    after = event.get("after") or {}
    if event_type in {"EVENT_ROLLED_BACK", "EVENT_REPLACED"}:
        return
    if entity_type == "character":
        current = state.setdefault("characters", {}).setdefault(entity_id, {})
        if isinstance(after, dict):
            current.update(after)
        state.setdefault("character_sources", {})[entity_id] = int(event.get("chapter_no") or 0)
    elif entity_type == "goal" or event_type.startswith("TASK_"):
        current = state.setdefault("goals", {}).setdefault(entity_id, {})
        if isinstance(after, dict):
            current.update(after)
    elif entity_type == "faction":
        current = state.setdefault("factions", {}).setdefault(entity_id, {})
        if isinstance(after, dict):
            current.update(after)
    elif entity_type in {"world", "location", "item", "relationship", "knowledge"}:
        bucket = state.setdefault(f"{entity_type}s", {})
        current = bucket.setdefault(entity_id, {})
        if isinstance(after, dict):
            current.update(after)
    else:
        state.setdefault("events", []).append({
            "id": event.get("id"), "event_type": event_type, "entity_type": entity_type,
            "entity_id": entity_id, "after": after, "chapter_no": event.get("chapter_no"),
        })


def replay_state(conn, work_id: str, chapter_limit: int | None = None, *, before: bool = False) -> dict[str, Any]:
    events = _event_rows(conn, work_id, chapter_limit, before=before)
    state: dict[str, Any] = {
        "characters": {}, "goals": {}, "factions": {}, "worlds": {}, "locations": {},
        "items": {}, "relationships": {}, "knowledges": {}, "events": [], "character_sources": {},
    }
    for event in events:
        _apply_event(state, event)
    state["fact_version"] = max((int(event.get("fact_version") or 0) for event in events), default=0)
    state["as_of_chapter"] = max((int(event.get("chapter_no") or 0) for event in events), default=0)
    return state


def get_story_state(work_id: str, *, before_chapter: int | None = None, at_chapter: int | None = None) -> dict[str, Any]:
    with transaction() as conn:
        if before_chapter is not None:
            return replay_state(conn, work_id, before_chapter, before=True)
        return replay_state(conn, work_id, at_chapter)


def rebuild_work_state(conn, work_id: str, from_chapter: int = 1) -> dict[str, Any]:
    """Replay confirmed events and refresh snapshots/read models."""
    conn.execute(
        "UPDATE story_snapshots SET valid=0 WHERE work_id=? AND chapter_no>=?",
        (work_id, int(from_chapter)),
    )
    chapters = conn.execute(
        """SELECT chapter_no FROM (
             SELECT chapter_no FROM chapter_plans WHERE work_id=?
             UNION SELECT chapter_no FROM chapters WHERE work_id=?
             UNION SELECT chapter_no FROM story_events WHERE work_id=?
           ) WHERE chapter_no>=? ORDER BY chapter_no""",
        (work_id, work_id, work_id, int(from_chapter)),
    ).fetchall()
    last_state: dict[str, Any] = replay_state(conn, work_id, from_chapter - 1)
    for row in chapters:
        chapter_no = int(row["chapter_no"])
        last_state = replay_state(conn, work_id, chapter_no)
        snapshot_id = str(uuid4())
        conn.execute(
            """INSERT INTO story_snapshots(id,work_id,chapter_no,fact_version,state_json,source_event_id,valid,created_at)
               VALUES (?,?,?,?,?,?,1,?)
               ON CONFLICT(work_id,chapter_no,fact_version) DO UPDATE SET state_json=excluded.state_json,valid=1,created_at=excluded.created_at""",
            (snapshot_id, work_id, chapter_no, int(last_state.get("fact_version") or 0), json_dumps(last_state), None, now_iso()),
        )
    latest = replay_state(conn, work_id, None)
    for character_id, state in latest.get("characters", {}).items():
        row = conn.execute(
            "SELECT id FROM character_states WHERE work_id=? AND character_id=?", (work_id, character_id)
        ).fetchone()
        source_chapter = int(latest.get("character_sources", {}).get(character_id) or 0)
        source_version = conn.execute(
            "SELECT id FROM chapter_versions WHERE work_id=? AND chapter_no=? ORDER BY version_no DESC LIMIT 1",
            (work_id, source_chapter),
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE character_states SET state_json=?,as_of_chapter=?,source_version_id=?,updated_at=? WHERE id=?",
                (json_dumps(state), source_chapter, source_version["id"] if source_version else None, now_iso(), row["id"]),
            )
        else:
            conn.execute(
                "INSERT INTO character_states(id,work_id,character_id,state_json,as_of_chapter,source_version_id,updated_at) VALUES (?,?,?,?,?,?,?)",
                (str(uuid4()), work_id, character_id, json_dumps(state), source_chapter, source_version["id"] if source_version else None, now_iso()),
            )
    conn.execute(
        "UPDATE works SET fact_version=?,updated_at=? WHERE id=?",
        (current_fact_version(conn, work_id), now_iso(), work_id),
    )
    return latest


def rollback_event(work_id: str, event_id: str, reason: str = "作者回滚事件") -> dict[str, Any] | None:
    with transaction() as conn:
        row = conn.execute(
            "SELECT * FROM story_events WHERE id=? AND work_id=? LIMIT 1", (event_id, work_id)
        ).fetchone()
        if not row:
            return None
        event = dict(row)
        if event.get("status") != "confirmed":
            raise ValueError("只有已确认事件可以回滚")
        conn.execute("UPDATE story_events SET status='reversed',reviewed_at=? WHERE id=?", (now_iso(), event_id))
        rollback = record_event(
            conn, work_id, chapter_no=int(event.get("chapter_no") or 0),
            chapter_version_id=event.get("chapter_version_id"), story_day=event.get("story_day"),
            event_type="EVENT_ROLLED_BACK", entity_type=event.get("entity_type", ""),
            entity_id=event.get("entity_id"), before=json_loads(event.get("after_json"), {}), after={},
            evidence=reason, risk_level="high", reversal_of=event_id,
        )
        conn.execute("UPDATE story_events SET replaced_by=? WHERE id=?", (rollback["id"], event_id))
        state = rebuild_work_state(conn, work_id, int(event.get("chapter_no") or 1))
        return {"rolled_back": event_id, "rollback_event": rollback, "state": state}


def supersede_chapter_version(conn, work_id: str, chapter_no: int, old_version_ids: list[str], new_version_id: str) -> None:
    """Remove facts produced by a rewritten chapter and replay its successors."""
    if not old_version_ids:
        return
    placeholders = ",".join("?" for _ in old_version_ids)
    rows = conn.execute(
        f"SELECT id FROM story_events WHERE work_id=? AND chapter_version_id IN ({placeholders}) AND status='confirmed'",
        (work_id, *old_version_ids),
    ).fetchall()
    if not rows:
        return
    conn.execute(
        f"UPDATE story_events SET status='superseded',replaced_by=?,reviewed_at=? WHERE id IN ({','.join('?' for _ in rows)})",
        (new_version_id, now_iso(), *(row["id"] for row in rows)),
    )
    conn.execute(
        f"UPDATE state_extractions SET status='superseded',superseded_by=? WHERE chapter_version_id IN ({placeholders}) AND status='applied'",
        (new_version_id, *old_version_ids),
    )
    record_event(
        conn, work_id, chapter_no=chapter_no, chapter_version_id=new_version_id, story_day=None,
        event_type="EVENT_REPLACED", entity_type="chapter_version", entity_id=new_version_id,
        before={"superseded_version_ids": old_version_ids}, after={}, evidence="章节正文已重写，旧版本事实撤销。",
        risk_level="high",
    )
    rebuild_work_state(conn, work_id, chapter_no)


def snapshot_for_chapter(work_id: str, chapter_no: int, *, before: bool = True) -> dict[str, Any]:
    return get_story_state(work_id, before_chapter=chapter_no if before else None, at_chapter=None if before else chapter_no)
