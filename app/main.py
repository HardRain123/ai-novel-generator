from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from app.config import WEB_ORIGIN
from app.db import init_db, transaction
from app.schemas import (
    ChapterUpdate,
    GenerateChapterRequest,
    GenerateOutlineRequest,
    StoryBibleUpdate,
    WorkCreate,
    WorkUpdate,
)
from app.services.novel_engine import engine
from app.services.repository import (
    create_work,
    get_work,
    list_works,
    save_chapter,
    save_outline,
    save_quality_report,
    save_story_setup,
    update_work,
)
from app.utils import json_dumps, now_iso


app = FastAPI(title="AI 长篇小说生成器", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[WEB_ORIGIN, "http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def startup():
    init_db()


def _work_or_404(work_id: str) -> dict[str, Any]:
    work = get_work(work_id)
    if not work:
        raise HTTPException(status_code=404, detail="作品不存在")
    return work


def _record_generation(conn, work_id: str, kind: str, input_data: dict, output_data: dict):
    import uuid

    conn.execute(
        "INSERT INTO generation_runs(id, work_id, kind, input_json, output_json, status, created_at) VALUES (?, ?, ?, ?, ?, 'completed', ?)",
        (str(uuid.uuid4()), work_id, kind, json_dumps(input_data), json_dumps(output_data), now_iso()),
    )


def _quality_check(work: dict[str, Any], chapter_no: int, content: str) -> tuple[list[dict[str, Any]], int]:
    issues: list[dict[str, Any]] = []
    text = content.strip()
    if not text:
        issues.append({"kind": "empty", "severity": "high", "message": "章节正文为空。", "suggestion": "先生成正文或补充本章场景。"})
    if len(text) < 180 and text:
        issues.append({"kind": "length", "severity": "low", "message": "本章内容较短，可能还没有完成完整的场景推进。", "evidence": f"当前约 {len(text)} 字。", "suggestion": "补充一个具体场景、阻力和结尾钩子。"})

    sentences = [line.strip() for line in text.replace("。", "。\n").splitlines() if line.strip()]
    repeated = [line for index, line in enumerate(sentences[1:], start=1) if line == sentences[index - 1]]
    if repeated:
        issues.append({"kind": "repetition", "severity": "medium", "message": "发现连续重复的句子。", "evidence": repeated[0][:100], "suggestion": "删除重复句或改成新的动作推进。"})

    names = [str(item.get("name")) for item in work.get("characters", []) if item.get("name")]
    if names and text and not any(name in text for name in names):
        issues.append({"kind": "character", "severity": "medium", "message": "本章没有检测到故事档案中的人物名称。", "suggestion": "确认是否写成了无人物主体的过渡段，或补充人物动作和对白。"})

    for item in work.get("foreshadows", []):
        expected = int(item.get("expected_reveal_chapter") or 0)
        if expected and expected <= chapter_no and item.get("status") == "open":
            issues.append({"kind": "foreshadow", "severity": "medium", "message": f"伏笔“{item.get('clue', '')}”预计在本章前回收，但仍处于未回收状态。", "suggestion": "确认本章是否需要回收，或调整预计回收章节。"})

    penalty = sum({"low": 5, "medium": 12, "high": 35}.get(issue["severity"], 0) for issue in issues)
    return issues, max(0, 100 - penalty)


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "ai-novel-generator"}


@app.get("/api/works")
def works():
    return {"items": list_works()}


@app.post("/api/works")
def create(payload: WorkCreate):
    return create_work(payload.model_dump())


@app.get("/api/works/{work_id}")
def work_detail(work_id: str):
    return _work_or_404(work_id)


@app.patch("/api/works/{work_id}")
def work_update(work_id: str, payload: WorkUpdate):
    updated = update_work(work_id, payload.model_dump())
    if not updated:
        raise HTTPException(status_code=404, detail="作品不存在")
    return updated


@app.put("/api/works/{work_id}/story-bible")
def update_story_bible(work_id: str, payload: StoryBibleUpdate):
    _work_or_404(work_id)
    with transaction() as conn:
        conn.execute(
            """
            UPDATE story_bibles SET summary=?, theme=?, world=?, ending=?, style_rules=?, locked=?, updated_at=?
            WHERE work_id=?
            """,
            (
                payload.summary, payload.theme, payload.world, payload.ending,
                payload.style_rules, int(payload.locked), now_iso(), work_id,
            ),
        )
        conn.execute("UPDATE works SET updated_at=? WHERE id=?", (now_iso(), work_id))
    return _work_or_404(work_id)


@app.post("/api/works/{work_id}/generate/setup")
def generate_setup(work_id: str):
    work = _work_or_404(work_id)
    data = engine.generate_setup(work)
    with transaction() as conn:
        save_story_setup(conn, work_id, data)
        _record_generation(conn, work_id, "story_setup", {}, data)
        conn.execute("UPDATE works SET status='planning', updated_at=? WHERE id=?", (now_iso(), work_id))
    return {"kind": "story_setup", "data": data, "work": _work_or_404(work_id)}


@app.post("/api/works/{work_id}/generate/outline")
def generate_outline(work_id: str, payload: GenerateOutlineRequest):
    work = _work_or_404(work_id)
    data = engine.generate_outline(work, payload.chapter_count)
    with transaction() as conn:
        save_outline(conn, work_id, data.get("chapters", []))
        _record_generation(conn, work_id, "outline", payload.model_dump(), data)
        conn.execute("UPDATE works SET status='planning', updated_at=? WHERE id=?", (now_iso(), work_id))
    return {"kind": "outline", "data": data, "work": _work_or_404(work_id)}


@app.post("/api/works/{work_id}/generate/chapter")
def generate_chapter(work_id: str, payload: GenerateChapterRequest):
    work = _work_or_404(work_id)
    data = engine.generate_chapter(work, payload.chapter_no, payload.mode, payload.instruction)
    issues, score = _quality_check({**work, "chapters": [*work.get("chapters", []), data]}, payload.chapter_no, data.get("content", ""))
    with transaction() as conn:
        save_chapter(conn, work_id, {**data, "status": "draft"})
        save_quality_report(conn, work_id, payload.chapter_no, issues, score)
        _record_generation(conn, work_id, "chapter", payload.model_dump(), data)
        conn.execute("UPDATE works SET status='writing', updated_at=? WHERE id=?", (now_iso(), work_id))
    return {"kind": "chapter", "data": data, "quality": {"score": score, "issues": issues}, "work": _work_or_404(work_id)}


@app.patch("/api/works/{work_id}/chapters/{chapter_no}")
def update_chapter(work_id: str, chapter_no: int, payload: ChapterUpdate):
    work = _work_or_404(work_id)
    current = next((item for item in work.get("chapters", []) if item.get("chapter_no") == chapter_no), None) or {}
    data = {
        "chapter_no": chapter_no,
        "title": payload.title if payload.title is not None else current.get("title", f"第{chapter_no}章"),
        "content": payload.content if payload.content is not None else current.get("content", ""),
        "status": payload.status if payload.status is not None else current.get("status", "draft"),
    }
    issues, score = _quality_check(work, chapter_no, data["content"])
    with transaction() as conn:
        save_chapter(conn, work_id, data)
        save_quality_report(conn, work_id, chapter_no, issues, score)
        conn.execute("UPDATE works SET updated_at=? WHERE id=?", (now_iso(), work_id))
    return {"chapter": data, "quality": {"score": score, "issues": issues}, "work": _work_or_404(work_id)}


@app.post("/api/works/{work_id}/quality/check")
def quality_check(work_id: str, chapter_no: int):
    work = _work_or_404(work_id)
    chapter = next((item for item in work.get("chapters", []) if item.get("chapter_no") == chapter_no), None)
    if not chapter:
        raise HTTPException(status_code=404, detail="章节不存在")
    issues, score = _quality_check(work, chapter_no, chapter.get("content", ""))
    with transaction() as conn:
        save_quality_report(conn, work_id, chapter_no, issues, score)
    return {"score": score, "issues": issues}

