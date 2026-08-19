from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from app.config import LLM_API_KEY, WEB_ORIGIN
from app.db import init_db, transaction
from app.schemas import (
    ChapterUpdate,
    GenerateChapterRequest,
    GenerateOutlineRequest,
    GenerationJobCreate,
    StoryBibleUpdate,
    StateReviewRequest,
    WorkCreate,
    WorkUpdate,
    CreateFromTrendIdeaRequest,
    ForeshadowCreate,
    ForeshadowUpdate,
    ModelProfileCreate,
    ModelProfileUpdate,
    TrendAnalyzeRequest,
    TrendSearchRequest,
)
from app.services.novel_engine import engine
from app.services.quality import quality_check as run_quality_check
from app.services.generation_jobs import cancel_job, enqueue_job, get_job, list_jobs, retry_job
from app.services.foreshadows import create_foreshadow, delete_foreshadow, foreshadow_stats, list_foreshadows, update_foreshadow
from app.services.model_profiles import bootstrap_legacy_profile, create_profile, delete_profile, fetch_models, get_profile, list_profiles, preset, test_profile, update_profile
from app.services.trends import SOURCE_CONFIG, analyze_trends, get_analysis, search_trends
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
from app.services.state_extraction import extract_and_persist, get_extraction, list_extractions, review_extraction
from app.utils import json_dumps, now_iso


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    bootstrap_legacy_profile()
    yield


app = FastAPI(title="AI 长篇小说生成器", version="0.2.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[WEB_ORIGIN, "http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


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


@app.get("/api/health")
def health():
    configured = bool(LLM_API_KEY or any(item.get("has_api_key") or (item.get("provider") == "codex_auth" and item.get("last_test_status") == "ok") for item in list_profiles()))
    return {"status": "ok", "service": "ai-novel-generator", "mode": "live" if configured else "demo", "model_configured": configured}


@app.get("/api/model-profiles")
def model_profiles():
    return {"items": list_profiles(), "presets": {key: preset(key) for key in ("deepseek", "qwen", "kimi", "custom", "codex_auth")}}


@app.post("/api/model-profiles")
def create_model_profile(payload: ModelProfileCreate):
    try:
        return create_profile(payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/model-profiles/{profile_id}")
def model_profile_detail(profile_id: str):
    profile = get_profile(profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="模型配置不存在")
    return profile


@app.patch("/api/model-profiles/{profile_id}")
def patch_model_profile(profile_id: str, payload: ModelProfileUpdate):
    try:
        profile = update_profile(profile_id, payload.model_dump(exclude_unset=True))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not profile:
        raise HTTPException(status_code=404, detail="模型配置不存在")
    return profile


@app.delete("/api/model-profiles/{profile_id}")
def remove_model_profile(profile_id: str):
    if not delete_profile(profile_id):
        raise HTTPException(status_code=404, detail="模型配置不存在")
    return {"ok": True}


@app.post("/api/model-profiles/{profile_id}/test")
def test_model_profile(profile_id: str):
    try:
        return test_profile(profile_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/model-profiles/{profile_id}/models")
def model_profile_models(profile_id: str):
    try:
        return {"items": fetch_models(profile_id)}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/works/{work_id}/generation-jobs", status_code=202)
def create_generation_job(work_id: str, payload: GenerationJobCreate):
    _work_or_404(work_id)
    try:
        return enqueue_job(work_id, payload.kind, payload.payload, payload.idempotency_key, payload.model_profile_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/works/{work_id}/generation-jobs/{job_id}")
def generation_job_detail(work_id: str, job_id: str):
    _work_or_404(work_id)
    job = get_job(job_id, work_id)
    if not job:
        raise HTTPException(status_code=404, detail="生成任务不存在")
    return job


@app.get("/api/works/{work_id}/generation-jobs")
def generation_jobs(work_id: str, active: bool = False):
    _work_or_404(work_id)
    return {"items": list_jobs(work_id, active)}


@app.post("/api/works/{work_id}/generation-jobs/{job_id}/cancel")
def cancel_generation_job(work_id: str, job_id: str):
    _work_or_404(work_id)
    try:
        job = cancel_job(job_id, work_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not job:
        raise HTTPException(status_code=404, detail="生成任务不存在")
    return job


@app.post("/api/works/{work_id}/generation-jobs/{job_id}/retry")
def retry_generation_job(work_id: str, job_id: str):
    _work_or_404(work_id)
    try:
        job = retry_job(job_id, work_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not job:
        raise HTTPException(status_code=404, detail="生成任务不存在")
    return job


@app.get("/api/works")
def works():
    return {"items": list_works()}


@app.post("/api/works")
def create(payload: WorkCreate):
    return create_work(payload.model_dump())


@app.get("/api/trends/sources")
def trend_sources():
    return {"items": [{"id": key, **value} for key, value in SOURCE_CONFIG.items()]}


@app.post("/api/trends/search")
def trend_search(payload: TrendSearchRequest):
    return search_trends(payload.sources, payload.category, payload.board, payload.keyword, payload.refresh)


@app.post("/api/trends/analyze")
def trend_analyze(payload: TrendAnalyzeRequest):
    try:
        return analyze_trends(payload.item_ids, payload.model_profile_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/trends/analyses/{analysis_id}")
def trend_analysis_detail(analysis_id: str):
    result = get_analysis(analysis_id)
    if not result:
        raise HTTPException(status_code=404, detail="趋势分析不存在")
    return result


@app.post("/api/works/from-trend-idea")
def create_work_from_trend(payload: CreateFromTrendIdeaRequest):
    analysis = get_analysis(payload.analysis_id)
    if not analysis:
        raise HTTPException(status_code=404, detail="趋势分析不存在")
    ideas = analysis.get("ideas") or []
    if payload.idea_index >= len(ideas):
        raise HTTPException(status_code=422, detail="创意不存在")
    idea = ideas[payload.idea_index]
    work = create_work({
        "title": idea.get("title", "未命名作品"),
        "genre": idea.get("genre", ""),
        "target_audience": idea.get("audience", ""),
        "premise": idea.get("premise", ""),
        "model_profile_id": payload.model_profile_id,
    })
    import uuid
    with transaction() as conn:
        for item in analysis.get("items", []):
            conn.execute("INSERT INTO work_inspirations(id,work_id,analysis_id,source,title,source_url,created_at) VALUES (?,?,?,?,?,?,?)", (str(uuid.uuid4()), work["id"], payload.analysis_id, item.get("source", ""), item.get("title", ""), item.get("source_url", ""), now_iso()))
    return work


@app.get("/api/works/{work_id}")
def work_detail(work_id: str):
    return _work_or_404(work_id)


@app.get("/api/works/{work_id}/foreshadows")
def work_foreshadows(work_id: str, status: str | None = None):
    work = _work_or_404(work_id)
    current_chapter = max((int(item.get("chapter_no") or 0) for item in work.get("chapters", [])), default=0) + 1
    return {"items": list_foreshadows(work_id, status), "stats": foreshadow_stats(work_id, current_chapter)}


@app.post("/api/works/{work_id}/foreshadows")
def add_work_foreshadow(work_id: str, payload: ForeshadowCreate):
    _work_or_404(work_id)
    return create_foreshadow(work_id, payload.model_dump())


@app.patch("/api/works/{work_id}/foreshadows/{item_id}")
def patch_work_foreshadow(work_id: str, item_id: str, payload: ForeshadowUpdate):
    _work_or_404(work_id)
    item = update_foreshadow(work_id, item_id, payload.model_dump(exclude_unset=True))
    if not item:
        raise HTTPException(status_code=404, detail="伏笔不存在")
    return item


@app.delete("/api/works/{work_id}/foreshadows/{item_id}")
def remove_work_foreshadow(work_id: str, item_id: str):
    _work_or_404(work_id)
    if not delete_foreshadow(work_id, item_id):
        raise HTTPException(status_code=404, detail="伏笔不存在")
    return {"ok": True}


@app.patch("/api/works/{work_id}")
def work_update(work_id: str, payload: WorkUpdate):
    updated = update_work(work_id, payload.model_dump(exclude_unset=True))
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
    issues, score = run_quality_check({**work, "chapters": [*work.get("chapters", []), data]}, payload.chapter_no, data.get("content", ""))
    with transaction() as conn:
        save_chapter(conn, work_id, {**data, "status": "draft"})
        save_quality_report(conn, work_id, payload.chapter_no, issues, score)
        _record_generation(conn, work_id, "chapter", payload.model_dump(), data)
        conn.execute("UPDATE works SET status='writing', updated_at=? WHERE id=?", (now_iso(), work_id))
    updated_work = _work_or_404(work_id)
    chapter = next(item for item in updated_work["chapters"] if item["chapter_no"] == payload.chapter_no)
    extraction = extract_and_persist(updated_work, chapter, "generation")
    return {"kind": "chapter", "data": data, "quality": {"score": score, "issues": issues}, "state_extraction": extraction, "work": updated_work}


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
    issues, score = run_quality_check(work, chapter_no, data["content"])
    with transaction() as conn:
        save_chapter(conn, work_id, data)
        save_quality_report(conn, work_id, chapter_no, issues, score)
        conn.execute("UPDATE works SET updated_at=? WHERE id=?", (now_iso(), work_id))
    updated_work = _work_or_404(work_id)
    chapter = next(item for item in updated_work["chapters"] if item["chapter_no"] == chapter_no)
    extraction = extract_and_persist(updated_work, chapter, "manual")
    return {"chapter": data, "quality": {"score": score, "issues": issues}, "state_extraction": extraction, "work": updated_work}


@app.post("/api/works/{work_id}/quality/check")
def quality_check(work_id: str, chapter_no: int):
    work = _work_or_404(work_id)
    chapter = next((item for item in work.get("chapters", []) if item.get("chapter_no") == chapter_no), None)
    if not chapter:
        raise HTTPException(status_code=404, detail="章节不存在")
    issues, score = run_quality_check(work, chapter_no, chapter.get("content", ""))
    with transaction() as conn:
        save_quality_report(conn, work_id, chapter_no, issues, score)
    return {"score": score, "issues": issues}


@app.get("/api/works/{work_id}/state-extractions")
def state_extractions(work_id: str, status: str | None = None):
    _work_or_404(work_id)
    return {"items": list_extractions(work_id, status)}


@app.get("/api/works/{work_id}/state-extractions/{extraction_id}")
def state_extraction_detail(work_id: str, extraction_id: str):
    _work_or_404(work_id)
    extraction = get_extraction(work_id, extraction_id)
    if not extraction:
        raise HTTPException(status_code=404, detail="状态提取记录不存在")
    return extraction


@app.post("/api/works/{work_id}/chapters/{chapter_no}/extract-state")
def extract_chapter_state(work_id: str, chapter_no: int):
    work = _work_or_404(work_id)
    chapter = next((item for item in work.get("chapters", []) if item.get("chapter_no") == chapter_no), None)
    if not chapter:
        raise HTTPException(status_code=404, detail="章节不存在")
    return extract_and_persist(work, chapter, "manual-rerun", force=True)


@app.post("/api/works/{work_id}/state-extractions/{extraction_id}/review")
def review_state_extraction(work_id: str, extraction_id: str, payload: StateReviewRequest):
    _work_or_404(work_id)
    try:
        result = review_extraction(
            work_id,
            extraction_id,
            [item.model_dump() for item in payload.items],
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="状态提取记录不存在")
    return result
