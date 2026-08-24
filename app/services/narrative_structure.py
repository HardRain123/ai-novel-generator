"""Long-form narrative structure helpers.

Timeline phases describe in-world time.  Volumes and narrative stages describe
story pacing.  Keeping them separate prevents labels such as ``建立`` from
being mistaken for a timeline phase key.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from app.utils import json_dumps, now_iso


DEFAULT_CHAPTER_WORDS = 2500
DEFAULT_DETAIL_WINDOW = 12
STAGE_TEMPLATES = (
    ("开局建立", "建立主角目标、基本规则和第一个可见回报", ["最终决战", "彻底击败最终对手"]),
    ("资源与关系积累", "补齐关键人物、资源和行动能力", ["卷末清算", "最终决战"]),
    ("首次试探", "让对手或风险以可承受代价首次施压", ["全面总攻", "彻底击败"]),
    ("冲突升级", "升级代价与反制，让主角改变策略", ["最终结局"]),
    ("危机反转", "形成卷末前必须解决的具体危机", ["全书结局"]),
    ("阶段兑现", "兑现本卷承诺，同时留下下一卷的新问题", []),
)


def suggested_target_chapters(estimated_words: int | None, average_chapter_words: int | None = None) -> int:
    words = max(1, int(estimated_words or 100000))
    average = max(800, int(average_chapter_words or DEFAULT_CHAPTER_WORDS))
    return max(1, min(10000, (words + average - 1) // average))


def narrative_location(work: dict[str, Any], chapter_no: int) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    volumes = work.get("story_volumes") or []
    stages = work.get("narrative_stages") or []
    volume = next(
        (item for item in volumes if int(item.get("start_chapter") or 1) <= chapter_no <= int(item.get("end_chapter") or 0)),
        None,
    )
    stage = next(
        (item for item in stages if int(item.get("start_chapter") or 1) <= chapter_no <= int(item.get("end_chapter") or 0)),
        None,
    )
    return volume, stage


def narrative_window(work: dict[str, Any], from_chapter: int, to_chapter: int) -> dict[str, Any]:
    volumes = [
        item for item in (work.get("story_volumes") or [])
        if int(item.get("end_chapter") or 0) >= from_chapter and int(item.get("start_chapter") or 1) <= to_chapter
    ]
    stages = [
        item for item in (work.get("narrative_stages") or [])
        if int(item.get("end_chapter") or 0) >= from_chapter and int(item.get("start_chapter") or 1) <= to_chapter
    ]
    return {"volumes": volumes, "narrative_stages": stages}


def _split_range(start: int, end: int, count: int) -> list[tuple[int, int]]:
    length = end - start + 1
    base, extra = divmod(length, count)
    cursor = start
    ranges: list[tuple[int, int]] = []
    for index in range(count):
        size = base + (1 if index < extra else 0)
        if size <= 0:
            break
        ranges.append((cursor, cursor + size - 1))
        cursor += size
    return ranges


def bootstrap_narrative_structure(conn, work_id: str, target_chapters: int, arcs: list[dict[str, Any]], *, replace: bool = False) -> None:
    """Create editable volume/stage coordinates from confirmed high-level arcs."""
    existing = conn.execute("SELECT 1 FROM story_volumes WHERE work_id=? LIMIT 1", (work_id,)).fetchone()
    if existing and not replace:
        return
    if replace:
        conn.execute("DELETE FROM narrative_stages WHERE work_id=?", (work_id,))
        conn.execute("DELETE FROM story_volumes WHERE work_id=?", (work_id,))
    arcs = sorted(arcs or [], key=lambda item: int(item.get("sequence") or 0))
    if not arcs:
        arcs = [{"title": "第一卷", "synopsis": "故事开局、发展与阶段性兑现", "sequence": 1}]
    now = now_iso()
    for sequence, (arc, (start, end)) in enumerate(zip(arcs, _split_range(1, target_chapters, len(arcs))), start=1):
        volume_id = str(uuid4())
        conn.execute(
            """INSERT INTO story_volumes(id,work_id,sequence,title,start_chapter,end_chapter,target_words,synopsis,goal,opposition,ending_state_json,status,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,'planned',?,?)""",
            (volume_id, work_id, sequence, str(arc.get("title") or f"第{sequence}卷"), start, end, 0,
             str(arc.get("synopsis") or ""), "", "", json_dumps({}), now, now),
        )
        for stage_sequence, ((title, purpose, forbidden), (stage_start, stage_end)) in enumerate(zip(STAGE_TEMPLATES, _split_range(start, end, len(STAGE_TEMPLATES))), start=1):
            conn.execute(
                """INSERT INTO narrative_stages(id,work_id,volume_id,sequence,title,start_chapter,end_chapter,purpose,entry_state_json,exit_state_json,allowed_payoffs_json,forbidden_payoffs_json,prerequisites_json,status,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,'{}','[]',?,'[]','planned',?,?)""",
                (str(uuid4()), work_id, volume_id, stage_sequence, title, stage_start, stage_end, purpose,
                 json_dumps({}), json_dumps(forbidden), now, now),
            )


def append_continuation_volume(conn, work_id: str, start_chapter: int, end_chapter: int) -> None:
    """Cover a later target expansion without moving previously outlined chapters."""
    if end_chapter < start_chapter:
        return
    sequence = int(conn.execute(
        "SELECT COALESCE(MAX(sequence), 0) AS sequence FROM story_volumes WHERE work_id=?", (work_id,)
    ).fetchone()["sequence"]) + 1
    now = now_iso()
    volume_id = str(uuid4())
    conn.execute(
        """INSERT INTO story_volumes(id,work_id,sequence,title,start_chapter,end_chapter,target_words,synopsis,goal,opposition,ending_state_json,status,created_at,updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,'planned',?,?)""",
        (volume_id, work_id, sequence, f"第{sequence}卷·续篇", start_chapter, end_chapter, 0,
         "承接已细化内容，继续推进主线并保留后续扩展空间。", "", "", json_dumps({}), now, now),
    )
    for stage_sequence, ((title, purpose, forbidden), (stage_start, stage_end)) in enumerate(
        zip(STAGE_TEMPLATES, _split_range(start_chapter, end_chapter, len(STAGE_TEMPLATES))), start=1
    ):
        conn.execute(
            """INSERT INTO narrative_stages(id,work_id,volume_id,sequence,title,start_chapter,end_chapter,purpose,entry_state_json,exit_state_json,allowed_payoffs_json,forbidden_payoffs_json,prerequisites_json,status,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,'{}','[]',?,'[]','planned',?,?)""",
            (str(uuid4()), work_id, volume_id, stage_sequence, title, stage_start, stage_end, purpose,
             json_dumps({}), json_dumps(forbidden), now, now),
        )
