from __future__ import annotations

from typing import Any
from uuid import uuid4

from app.db import transaction
from app.utils import now_iso


def list_foreshadows(work_id: str, status: str | None = None) -> list[dict[str, Any]]:
    with transaction() as conn:
        if status:
            rows = conn.execute("SELECT * FROM foreshadows WHERE work_id=? AND status=? ORDER BY expected_reveal_chapter, planted_chapter", (work_id, status)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM foreshadows WHERE work_id=? ORDER BY expected_reveal_chapter, planted_chapter", (work_id,)).fetchall()
        return [dict(row) for row in rows]


def get_foreshadow(work_id: str, item_id: str) -> dict[str, Any] | None:
    with transaction() as conn:
        row = conn.execute("SELECT * FROM foreshadows WHERE id=? AND work_id=?", (item_id, work_id)).fetchone()
        return dict(row) if row else None


def create_foreshadow(work_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    item_id = str(uuid4())
    now = now_iso()
    with transaction() as conn:
        conn.execute("""INSERT INTO foreshadows(id,work_id,clue,kind,planted_chapter,expected_reveal_chapter,status,
            actual_reveal_chapter,note,evidence,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                     (item_id, work_id, payload["clue"].strip(), payload.get("kind", "clue"), payload.get("planted_chapter", 0),
                      payload.get("expected_reveal_chapter", 0), payload.get("status", "open"), payload.get("actual_reveal_chapter", 0),
                      payload.get("note", ""), payload.get("evidence", ""), now, now))
    return get_foreshadow(work_id, item_id)  # type: ignore[return-value]


def update_foreshadow(work_id: str, item_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    allowed = {key: value for key, value in payload.items() if value is not None and key in {
        "clue", "kind", "planted_chapter", "expected_reveal_chapter", "status", "actual_reveal_chapter", "note", "evidence"
    }}
    if not allowed:
        return get_foreshadow(work_id, item_id)
    allowed["updated_at"] = now_iso()
    assignments = ", ".join(f"{key}=?" for key in allowed)
    with transaction() as conn:
        cur = conn.execute(f"UPDATE foreshadows SET {assignments} WHERE id=? AND work_id=?", (*allowed.values(), item_id, work_id))
        if not cur.rowcount:
            return None
    return get_foreshadow(work_id, item_id)


def delete_foreshadow(work_id: str, item_id: str) -> bool:
    with transaction() as conn:
        return conn.execute("DELETE FROM foreshadows WHERE id=? AND work_id=?", (item_id, work_id)).rowcount > 0


def foreshadow_stats(work_id: str, current_chapter: int = 0) -> dict[str, int]:
    items = list_foreshadows(work_id)
    open_items = [item for item in items if item.get("status") == "open"]
    overdue = [item for item in open_items if int(item.get("expected_reveal_chapter") or 0) and int(item["expected_reveal_chapter"]) < current_chapter]
    soon = [item for item in open_items if int(item.get("expected_reveal_chapter") or 0) and current_chapter <= int(item["expected_reveal_chapter"]) <= current_chapter + 3]
    return {"total": len(items), "open": len(open_items), "soon": len(soon), "overdue": len(overdue), "revealed": sum(item.get("status") == "revealed" for item in items)}
