from contextlib import asynccontextmanager
import json
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse

from app.config import DESKTOP_MODE, LLM_API_KEY, WEB_ORIGIN
from app.db import init_db, transaction
from app.schemas import (
    ChapterUpdate,
    CharacterUpdate,
    ChapterConfirmRequest,
    ChapterPlanUpdate,
    EventRollbackRequest,
    FactionUpdate,
    FuturePlanCreate,
    FuturePlanUpdate,
    GenerateChapterRequest,
    GenerateOutlineRequest,
    GenerationJobCreate,
    PlanningCharacterBatchRequest,
    PlanningArtifactConfirm,
    PlanningArtifactUpdate,
    PlanningResetRequest,
    PlanningStepGenerateRequest,
    StoryBibleUpdate,
    StoryVolumeUpdate,
    StoryGoalCreate,
    StoryGoalUpdate,
    StoryPhaseUpdate,
    LongTermFactUpsert,
    StateReviewRequest,
    WorkCreate,
    WorkUpdate,
    CreateFromInspirationBlueprintRequest,
    CreateFromTrendIdeaRequest,
    ForeshadowCreate,
    ForeshadowUpdate,
    ModelProfileCreate,
    ModelProfileUpdate,
    NarrativeStageUpdate,
    NarrativeStructureBootstrap,
    PromptSettingUpdate,
    ProxySettingsUpdate,
    TrendAnalyzeRequest,
    TrendSearchRequest,
)
from app.services.novel_engine import PLANNING_PRESETS, PROMPT_DEFAULTS, engine
from app.services.app_settings import get_proxy_settings, save_proxy_settings, test_proxy_port
from app.services.prompt_settings import list_prompt_settings, reset_prompt_setting, save_prompt_setting
from app.services.quality import quality_check as run_quality_check
from app.services.generation_jobs import cancel_job, enqueue_job, enqueue_state_extraction, get_job, list_jobs, retry_job
from app.services.planning_repository import (
    STEP_ORDER,
    confirm_artifact,
    finalize_planning,
    get_planning_session,
    list_planning_snapshots,
    prerequisite_error,
    reset_planning,
    restore_planning_snapshot,
    update_artifact_content,
)
from app.services.foreshadows import create_foreshadow, delete_foreshadow, foreshadow_stats, list_foreshadows, update_foreshadow
from app.services.model_profiles import bootstrap_legacy_profile, codex_auth_status, create_profile, delete_profile, fetch_models, get_profile, list_profiles, preset, profile_for_task, resolve_profile, test_profile, update_profile
from app.services.model_call_logs import get_model_call, list_model_calls, model_call_stats
from app.services.planning_quality import planning_field_rules
from app.services.trends import SOURCE_CONFIG, analyze_trends, get_analysis, get_blueprint, search_trends
from app.services.repository import (
    create_work,
    delete_work,
    get_work,
    list_works,
    save_chapter,
    save_outline,
    save_quality_report,
    save_story_setup,
    export_complete_outline,
    list_chapter_plan_history,
    list_outline_versions,
    update_chapter_plan,
    update_character_card,
    update_work,
    ensure_long_form_structure,
)
from app.services.state_extraction import get_extraction, list_extractions, review_extraction
from app.services.story_state import validate_chapter_generation, validate_chapter_plan
from app.services.context_builder import build_context, build_chapter_generation_context, latest_context_audit
from app.services.outline_engine import ensure_lifecycle_candidates, generate_outline_batches
from app.services.narrative_structure import bootstrap_narrative_structure, suggested_target_chapters
from app.services.state_engine import get_story_state, record_event, rollback_event, rebuild_work_state
from app.utils import json_dumps, now_iso


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    bootstrap_legacy_profile()
    yield


app = FastAPI(title="AI 长篇小说生成器", version="0.2.0", lifespan=lifespan)
local_web_origins = ["http://localhost:3000", "http://127.0.0.1:3000"]
if DESKTOP_MODE:
    # Electron loads the static SPA from file://, which browsers report as a
    # null origin. The API is bound to loopback by the desktop launcher.
    local_web_origins.extend(["null", "file://"])
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(dict.fromkeys([WEB_ORIGIN, *local_web_origins])),
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


@app.get("/api/planning-rules")
def planning_rules():
    """Expose the planning editor's server-side quality contract."""
    return planning_field_rules()


@app.get("/api/model-profiles")
def model_profiles():
    return {
        "items": list_profiles(),
        "presets": {key: preset(key) for key in ("deepseek", "qwen", "kimi", "custom", "codex_auth")},
        "codex_auth": codex_auth_status(),
    }


@app.get("/api/settings/proxy")
def proxy_settings():
    settings = get_proxy_settings()
    return {**settings, **test_proxy_port(settings["port"])}


@app.put("/api/settings/proxy")
def update_proxy_settings(payload: ProxySettingsUpdate):
    settings = save_proxy_settings(payload.enabled, payload.port)
    return {**settings, **test_proxy_port(settings["port"])}


@app.post("/api/settings/proxy/test")
def test_proxy_settings(payload: ProxySettingsUpdate):
    return test_proxy_port(payload.port)


@app.get("/api/settings/prompts")
def prompt_settings():
    return {"items": list_prompt_settings(PROMPT_DEFAULTS)}


@app.put("/api/settings/prompts/{prompt_key}")
def update_prompt_setting(prompt_key: str, payload: PromptSettingUpdate):
    try:
        save_prompt_setting(prompt_key, payload.content)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="提示词阶段不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return next(item for item in list_prompt_settings(PROMPT_DEFAULTS) if item["key"] == prompt_key)


@app.delete("/api/settings/prompts/{prompt_key}")
def restore_prompt_setting(prompt_key: str):
    try:
        reset_prompt_setting(prompt_key)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="提示词阶段不存在") from exc
    return next(item for item in list_prompt_settings(PROMPT_DEFAULTS) if item["key"] == prompt_key)


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


@app.get("/api/model-call-logs")
def model_call_log_list(
    limit: int = 50,
    offset: int = 0,
    status: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    call_kind: str | None = None,
    work_id: str | None = None,
    from_at: str | None = None,
    to_at: str | None = None,
):
    try:
        return list_model_calls(
            limit=limit,
            offset=offset,
            status=status,
            provider=provider,
            model=model,
            call_kind=call_kind,
            work_id=work_id,
            from_at=from_at,
            to_at=to_at,
        )
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="日志查询参数无效") from exc


@app.get("/api/model-call-logs/stats")
def model_call_log_stats(work_id: str | None = None):
    return model_call_stats(work_id=work_id)


@app.get("/api/model-call-logs/{call_id}")
def model_call_log_detail(call_id: str):
    result = get_model_call(call_id)
    if not result:
        raise HTTPException(status_code=404, detail="模型调用记录不存在")
    return result


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


@app.get("/api/works/{work_id}/planning-session")
def planning_session(work_id: str):
    _work_or_404(work_id)
    result = get_planning_session(work_id)
    result["presets"] = {key: {"key": key, **value} for key, value in PLANNING_PRESETS.items()}
    return result


@app.post("/api/works/{work_id}/planning-session/reset")
def reset_planning_session(work_id: str, payload: PlanningResetRequest):
    _work_or_404(work_id)
    if not payload.confirm:
        raise HTTPException(status_code=400, detail="请明确确认重置故事规划")
    try:
        return reset_planning(work_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/works/{work_id}/planning-steps/{step}/generate", status_code=202)
def generate_planning_step(work_id: str, step: str, payload: PlanningStepGenerateRequest):
    _work_or_404(work_id)
    if step not in STEP_ORDER:
        raise HTTPException(status_code=422, detail="不支持的规划步骤")
    missing = prerequisite_error(work_id, step)
    if missing:
        raise HTTPException(status_code=409, detail=missing)
    active = [item for item in list_jobs(work_id, True) if item.get("kind") == "planning_step"]
    if active:
        raise HTTPException(status_code=409, detail="当前作品已有一个规划步骤正在生成")
    item_key = payload.item_key or ("contract" if step == "contract" else "default")
    candidate_count = 3 if step == "contract" else 1
    job_payload = {
        "step": step,
        "item_key": item_key,
        "feedback": payload.feedback,
        "preset": payload.preset or "custom",
        "candidate_count": candidate_count,
    }
    try:
        return enqueue_job(
            work_id,
            "planning_step",
            job_payload,
            idempotency_key=payload.idempotency_key or f"planning-{step}-{item_key}-{hash(payload.feedback)}",
            model_profile_id=payload.model_profile_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/works/{work_id}/planning-steps/character/generate-all", status_code=202)
def generate_all_character_biographies(work_id: str, payload: PlanningCharacterBatchRequest):
    """Generate missing or failed character drafts without touching confirmed cards."""
    _work_or_404(work_id)
    missing = prerequisite_error(work_id, "character")
    if missing:
        raise HTTPException(status_code=409, detail=missing)
    active = [
        item for item in list_jobs(work_id, True)
        if item.get("kind") in {"planning_step", "planning_character_batch"}
    ]
    if active:
        raise HTTPException(status_code=409, detail="当前作品已有一个规划步骤正在生成")
    session = get_planning_session(work_id)
    roster = next(
        (
            item.get("content", {}).get("characters", [])
            for item in session["artifacts"]
            if item["step"] == "cast_roster" and item["status"] == "confirmed"
        ),
        [],
    )
    existing = {
        item["item_key"]: item
        for item in session["artifacts"]
        if item["step"] == "character"
    }
    def needs_generation(item_key: str) -> bool:
        artifact = existing.get(item_key)
        if not artifact:
            return True
        checks = artifact.get("checks") if isinstance(artifact.get("checks"), dict) else {}
        return artifact.get("status") == "draft" and bool(checks.get("blocking"))

    item_keys = [
        str(item.get("item_key")) for item in roster
        if isinstance(item, dict) and item.get("item_key") and needs_generation(str(item.get("item_key")))
    ]
    if not item_keys:
        raise HTTPException(status_code=409, detail="所有角色都已有通过质检的草稿或已确认；如需修改，请使用单个角色的重新生成")
    draft_versions = tuple((item_key, int((existing.get(item_key) or {}).get("version") or 0)) for item_key in item_keys)
    try:
        return enqueue_job(
            work_id,
            "planning_character_batch",
            {"item_keys": item_keys, "feedback": payload.feedback, "preset": payload.preset or "custom"},
            idempotency_key=payload.idempotency_key or f"planning-character-batch-{hash(draft_versions)}-{hash(payload.feedback)}",
            model_profile_id=payload.model_profile_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.put("/api/works/{work_id}/planning-steps/{step}/{item_key}")
def update_planning_artifact(work_id: str, step: str, item_key: str, payload: PlanningArtifactUpdate):
    _work_or_404(work_id)
    if step not in STEP_ORDER:
        raise HTTPException(status_code=422, detail="不支持的规划步骤")
    return update_artifact_content(work_id, step, item_key, payload.content, payload.feedback)


@app.get("/api/works/{work_id}/planning-snapshots")
def planning_snapshots(work_id: str, step: str, item_key: str = "default"):
    _work_or_404(work_id)
    if step not in STEP_ORDER:
        raise HTTPException(status_code=422, detail="不支持的规划步骤")
    return {"items": list_planning_snapshots(work_id, step, item_key)}


@app.post("/api/works/{work_id}/planning-snapshots/{snapshot_id}/restore")
def restore_snapshot(work_id: str, snapshot_id: str):
    _work_or_404(work_id)
    try:
        return restore_planning_snapshot(work_id, snapshot_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/works/{work_id}/planning-steps/{step}/{item_key}/confirm")
def confirm_planning_artifact(work_id: str, step: str, item_key: str, payload: PlanningArtifactConfirm):
    _work_or_404(work_id)
    if step not in STEP_ORDER:
        raise HTTPException(status_code=422, detail="不支持的规划步骤")
    if payload.candidate_index is not None:
        session = get_planning_session(work_id)
        item = next((item for item in session["artifacts"] if item["step"] == step and item["item_key"] == item_key), None)
        candidates = (item or {}).get("content", {}).get("candidates", [])
        if payload.candidate_index >= len(candidates):
            raise HTTPException(status_code=422, detail="候选方向不存在")
        content = dict(item["content"])
        content["selected"] = candidates[payload.candidate_index]
        update_artifact_content(work_id, step, item_key, content)
    try:
        return confirm_artifact(work_id, step, item_key)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/works/{work_id}/planning-session/finalize")
def finalize_planning_session(work_id: str):
    _work_or_404(work_id)
    try:
        data = finalize_planning(work_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"kind": "story_setup", "data": data, "work": _work_or_404(work_id)}


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
    blueprint_id = idea.get("blueprint_id") if isinstance(idea, dict) else None
    if not blueprint_id:
        raise HTTPException(status_code=409, detail="该分析没有可用的原创蓝图，请重新分析榜单作品")
    return _create_work_from_blueprint(str(blueprint_id), payload.model_profile_id, payload.idempotency_key)


def _create_work_from_blueprint(blueprint_id: str, model_profile_id: str | None, idempotency_key: str | None = None):
    blueprint = get_blueprint(blueprint_id)
    if not blueprint:
        raise HTTPException(status_code=404, detail="原创蓝图不存在")
    if idempotency_key:
        with transaction() as conn:
            existing = conn.execute(
                "SELECT work_id FROM work_inspiration_blueprints WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
        if existing:
            return _work_or_404(existing["work_id"])
    content = blueprint.get("content") if isinstance(blueprint.get("content"), dict) else {}
    work = create_work({
        "title": content.get("title") or "未命名作品",
        "genre": content.get("genre") or "",
        "target_audience": content.get("audience") or "",
        "premise": content.get("premise") or "",
        "model_profile_id": model_profile_id,
    })
    analysis = get_analysis(str(blueprint["analysis_id"])) or {}
    import uuid
    with transaction() as conn:
        for item in analysis.get("items", []):
            conn.execute(
                "INSERT INTO work_inspirations(id,work_id,analysis_id,source,title,source_url,created_at) VALUES (?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), work["id"], blueprint["analysis_id"], item.get("source", ""), item.get("title", ""), item.get("source_url", ""), now_iso()),
            )
        conn.execute(
            "INSERT INTO work_inspiration_blueprints(id,work_id,blueprint_id,idempotency_key,created_at) VALUES (?,?,?,?,?)",
            (str(uuid.uuid4()), work["id"], blueprint_id, idempotency_key, now_iso()),
        )
    return _work_or_404(work["id"])


@app.post("/api/works/from-inspiration-blueprint")
def create_work_from_inspiration_blueprint(payload: CreateFromInspirationBlueprintRequest):
    return _create_work_from_blueprint(payload.blueprint_id, payload.model_profile_id, payload.idempotency_key)


@app.get("/api/inspiration-blueprints/{blueprint_id}")
def inspiration_blueprint_detail(blueprint_id: str):
    blueprint = get_blueprint(blueprint_id)
    if not blueprint:
        raise HTTPException(status_code=404, detail="原创蓝图不存在")
    return blueprint


@app.get("/api/works/{work_id}")
def work_detail(work_id: str):
    return _work_or_404(work_id)


@app.delete("/api/works/{work_id}")
def remove_work(work_id: str):
    try:
        deleted = delete_work(work_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="作品不存在")
    return {"ok": True, "id": work_id}


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
    if payload.model_profile_id:
        profile = get_profile(payload.model_profile_id)
        if not profile or not profile.get("enabled", True):
            raise HTTPException(status_code=422, detail="选择的模型配置不存在或已停用")
    updated = update_work(work_id, payload.model_dump(exclude_unset=True))
    if not updated:
        raise HTTPException(status_code=404, detail="作品不存在")
    return updated


@app.post("/api/works/{work_id}/narrative-structure/bootstrap")
def bootstrap_narrative_structure_route(work_id: str, payload: NarrativeStructureBootstrap):
    work = _work_or_404(work_id)
    target = int(work.get("target_chapter_count") or suggested_target_chapters(work.get("estimated_words"), work.get("average_chapter_words")))
    target = max(target, max((int(item.get("end_chapter") or 0) for item in work.get("story_volumes", [])), default=0))
    with transaction() as conn:
        bootstrap_narrative_structure(conn, work_id, target, work.get("plot_arcs") or [], replace=payload.replace)
        conn.execute("UPDATE works SET target_chapter_count=?, updated_at=? WHERE id=?", (target, now_iso(), work_id))
    return _work_or_404(work_id)


@app.put("/api/works/{work_id}/story-volumes/{volume_id}")
def upsert_story_volume(work_id: str, volume_id: str, payload: StoryVolumeUpdate):
    work = _work_or_404(work_id)
    if payload.end_chapter < payload.start_chapter:
        raise HTTPException(status_code=422, detail="分卷结束章节不能早于开始章节")
    # A legacy request may have lowered target_chapter_count to a short outline
    # batch (for example 12) while the authored volume already spans 1—40.
    # The persisted volume coordinates are authoritative for that lower bound.
    structure_end = max((int(item.get("end_chapter") or 0) for item in work.get("story_volumes", [])), default=0)
    effective_target = max(int(work.get("target_chapter_count") or 0), structure_end)
    if payload.end_chapter > effective_target:
        raise HTTPException(status_code=422, detail="分卷范围不能超过全书目标章节数")
    now = now_iso()
    with transaction() as conn:
        conn.execute(
            """INSERT INTO story_volumes(id,work_id,sequence,title,start_chapter,end_chapter,target_words,synopsis,goal,opposition,ending_state_json,status,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET sequence=excluded.sequence,title=excluded.title,start_chapter=excluded.start_chapter,end_chapter=excluded.end_chapter,target_words=excluded.target_words,synopsis=excluded.synopsis,goal=excluded.goal,opposition=excluded.opposition,ending_state_json=excluded.ending_state_json,status=excluded.status,updated_at=excluded.updated_at""",
            (volume_id, work_id, payload.sequence, payload.title, payload.start_chapter, payload.end_chapter,
             payload.target_words, payload.synopsis, payload.goal, payload.opposition, json_dumps(payload.ending_state),
             payload.status, now, now),
        )
        if int(work.get("target_chapter_count") or 0) < effective_target:
            conn.execute("UPDATE works SET target_chapter_count=?, updated_at=? WHERE id=?", (effective_target, now, work_id))
    return _work_or_404(work_id)


@app.put("/api/works/{work_id}/narrative-stages/{stage_id}")
def upsert_narrative_stage(work_id: str, stage_id: str, payload: NarrativeStageUpdate):
    work = _work_or_404(work_id)
    if payload.end_chapter < payload.start_chapter:
        raise HTTPException(status_code=422, detail="叙事阶段结束章节不能早于开始章节")
    volume = next((item for item in work.get("story_volumes", []) if item.get("id") == payload.volume_id), None)
    if not volume:
        raise HTTPException(status_code=422, detail="所属分卷不存在")
    if payload.start_chapter < int(volume["start_chapter"]) or payload.end_chapter > int(volume["end_chapter"]):
        raise HTTPException(status_code=422, detail="叙事阶段必须完全位于所属分卷范围内")
    now = now_iso()
    with transaction() as conn:
        conn.execute(
            """INSERT INTO narrative_stages(id,work_id,volume_id,sequence,title,start_chapter,end_chapter,purpose,entry_state_json,exit_state_json,allowed_payoffs_json,forbidden_payoffs_json,prerequisites_json,status,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET volume_id=excluded.volume_id,sequence=excluded.sequence,title=excluded.title,start_chapter=excluded.start_chapter,end_chapter=excluded.end_chapter,purpose=excluded.purpose,entry_state_json=excluded.entry_state_json,exit_state_json=excluded.exit_state_json,allowed_payoffs_json=excluded.allowed_payoffs_json,forbidden_payoffs_json=excluded.forbidden_payoffs_json,prerequisites_json=excluded.prerequisites_json,status=excluded.status,updated_at=excluded.updated_at""",
            (stage_id, work_id, payload.volume_id, payload.sequence, payload.title, payload.start_chapter,
             payload.end_chapter, payload.purpose, json_dumps(payload.entry_state), json_dumps(payload.exit_state),
             json_dumps(payload.allowed_payoffs), json_dumps(payload.forbidden_payoffs), json_dumps(payload.prerequisites), payload.status, now, now),
        )
    return _work_or_404(work_id)


@app.put("/api/works/{work_id}/story-bible")
def update_story_bible(work_id: str, payload: StoryBibleUpdate):
    _work_or_404(work_id)
    with transaction() as conn:
        conn.execute(
            """
            UPDATE story_bibles SET summary=?, theme=?, world=?, ending=?, style_rules=?,
                title_interpretation=?, reader_promise=?, core_hook=?, core_conflict=?, stakes=?,
                must_have_elements_json=?, avoid_drift_json=?, locked=?, updated_at=?
            WHERE work_id=?
            """,
            (
                payload.summary, payload.theme, payload.world, payload.ending, payload.style_rules,
                payload.title_interpretation, payload.reader_promise, payload.core_hook,
                payload.core_conflict, payload.stakes, json_dumps(payload.must_have_elements),
                json_dumps(payload.avoid_drift), int(payload.locked), now_iso(), work_id,
            ),
        )
        conn.execute("UPDATE works SET updated_at=? WHERE id=?", (now_iso(), work_id))
    return _work_or_404(work_id)


@app.post("/api/works/{work_id}/generate/setup")
def generate_setup(work_id: str):
    work = _work_or_404(work_id)
    profile = profile_for_task(resolve_profile(work_id=work_id), "setup")
    try:
        data = engine.generate_setup(work, profile)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    with transaction() as conn:
        save_story_setup(conn, work_id, data)
        _record_generation(conn, work_id, "story_setup", {}, data)
        conn.execute("UPDATE works SET status='planning', updated_at=? WHERE id=?", (now_iso(), work_id))
    return {"kind": "story_setup", "data": data, "work": _work_or_404(work_id)}


@app.post("/api/works/{work_id}/generate/outline")
def generate_outline(work_id: str, payload: GenerateOutlineRequest):
    _work_or_404(work_id)
    ensure_lifecycle_candidates(work_id)
    requested_target = payload.total_target_chapters or payload.chapter_count
    ensure_long_form_structure(work_id, requested_target)
    work = _work_or_404(work_id)
    profile = profile_for_task(resolve_profile(work_id=work_id), "outline")
    request = payload.model_dump()
    try:
        data = generate_outline_batches(work, request, profile)
        validation_work = {
            **work,
            "chapter_plans": [item for item in work.get("chapter_plans", []) if int(item.get("chapter_no") or 0) < data["from_chapter"]],
        }
        for item in data["chapters"]:
            errors = validate_chapter_plan(validation_work, item, replacing_no=int(item["chapter_no"]))
            if errors:
                raise ValueError("；".join(errors))
            validation_work["chapter_plans"].append(item)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    try:
        with transaction() as conn:
            saved = save_outline(
                conn, work_id, data.get("chapters", []), mode=data["mode"],
                from_chapter=data["from_chapter"], to_chapter=data["to_chapter"],
                request={**request, **({"repair_history": data["repair_history"]} if data.get("repair_history") else {})},
                expected_outline_version=payload.expected_outline_version,
                expected_fact_version=data["fact_version"] if payload.expected_fact_version is None else payload.expected_fact_version,
            )
            data.update(saved)
            _record_generation(conn, work_id, "outline", request, data)
            conn.execute("UPDATE works SET status='planning', target_chapter_count=?, updated_at=? WHERE id=?", (data["total_target_chapters"], now_iso(), work_id))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"kind": "outline", "data": data, "work": _work_or_404(work_id)}


@app.post("/api/works/{work_id}/generate/chapter")
def generate_chapter(work_id: str, payload: GenerateChapterRequest):
    work = _work_or_404(work_id)
    timeline_errors = validate_chapter_generation(work, payload.chapter_no)
    if timeline_errors:
        raise HTTPException(status_code=409, detail="；".join(timeline_errors))
    profile = profile_for_task(resolve_profile(work_id=work_id), "chapter")
    data = engine.generate_chapter(work, payload.chapter_no, payload.mode, payload.instruction, profile)
    issues, score = run_quality_check({**work, "chapters": [*work.get("chapters", []), data]}, payload.chapter_no, data.get("content", ""))
    with transaction() as conn:
        save_chapter(conn, work_id, {**data, "status": "draft"})
        save_quality_report(conn, work_id, payload.chapter_no, issues, score)
        _record_generation(conn, work_id, "chapter", payload.model_dump(), data)
        conn.execute("UPDATE works SET status='writing', updated_at=? WHERE id=?", (now_iso(), work_id))
    updated_work = _work_or_404(work_id)
    chapter = next(item for item in updated_work["chapters"] if item["chapter_no"] == payload.chapter_no)
    extraction = enqueue_state_extraction(updated_work, chapter, "generation", profile=profile)
    return {"kind": "chapter", "data": data, "quality": {"score": score, "issues": issues}, "state_extraction": extraction, "work": updated_work}


@app.put("/api/works/{work_id}/chapter-plans/{chapter_no}")
def update_chapter_plan_route(work_id: str, chapter_no: int, payload: ChapterPlanUpdate):
    work = _work_or_404(work_id)
    current = next((item for item in work.get("chapter_plans", []) if int(item.get("chapter_no") or 0) == chapter_no), None)
    if not current:
        raise HTTPException(status_code=404, detail="章节大纲不存在")
    changes = payload.model_dump(exclude_none=True)
    expected_version = changes.pop("expected_version", None)
    if expected_version is not None and int(expected_version) != int(current.get("version") or 0):
        raise HTTPException(status_code=409, detail="大纲版本已变化，请刷新后再保存")
    updated = {**current, **changes, "chapter_no": chapter_no}
    errors = validate_chapter_plan(work, updated, replacing_no=chapter_no)
    if errors:
        raise HTTPException(status_code=409, detail="；".join(errors))
    with transaction() as conn:
        update_chapter_plan(conn, work_id, chapter_no, changes)
        conn.execute("UPDATE works SET updated_at=? WHERE id=?", (now_iso(), work_id))
    return _work_or_404(work_id)


@app.put("/api/works/{work_id}/characters/{character_id}")
def update_character(work_id: str, character_id: str, payload: CharacterUpdate):
    _work_or_404(work_id)
    try:
        with transaction() as conn:
            impact = update_character_card(conn, work_id, character_id, payload.model_dump(exclude_unset=True))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"work": _work_or_404(work_id), "impact": impact}


@app.get("/api/works/{work_id}/story-state")
def story_state(work_id: str, chapter_no: int | None = None, before_chapter: bool = True):
    work = _work_or_404(work_id)
    canonical = get_story_state(
        work_id,
        before_chapter=chapter_no if chapter_no is not None and before_chapter else None,
        at_chapter=chapter_no if chapter_no is not None and not before_chapter else None,
    )
    return {
        "phases": work.get("story_phases", []), "factions": work.get("factions", []),
        "goals": work.get("goals", []), "events": work.get("story_events", []),
        "character_states": work.get("character_states", []),
        "canonical_state": canonical,
        "fact_version": work.get("fact_version", 0),
        "as_of": {"chapter_no": chapter_no, "before_chapter": before_chapter},
    }


@app.put("/api/works/{work_id}/story-phases/{phase_key}")
def upsert_story_phase(work_id: str, phase_key: str, payload: StoryPhaseUpdate):
    _work_or_404(work_id)
    if phase_key != payload.phase_key:
        raise HTTPException(status_code=422, detail="路径中的阶段标识必须与请求一致")
    if payload.end_day is not None and payload.start_day is not None and payload.end_day < payload.start_day:
        raise HTTPException(status_code=422, detail="阶段结束时间不能早于开始时间")
    now = now_iso()
    with transaction() as conn:
        conn.execute(
            """INSERT INTO story_phases(id,work_id,phase_key,name,start_day,end_day,rules_json,locked,created_at,updated_at,allowed_json,forbidden_json,transition_conditions_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(work_id,phase_key) DO UPDATE SET name=excluded.name,start_day=excluded.start_day,
                 end_day=excluded.end_day,rules_json=excluded.rules_json,locked=excluded.locked,
                 allowed_json=excluded.allowed_json,forbidden_json=excluded.forbidden_json,
                 transition_conditions_json=excluded.transition_conditions_json,updated_at=excluded.updated_at""",
            (str(uuid4()), work_id, phase_key, payload.name, payload.start_day, payload.end_day,
             json_dumps(payload.rules), int(payload.locked), now, now, json_dumps(payload.allowed),
             json_dumps(payload.forbidden), json_dumps(payload.transition_conditions)),
        )
        record_event(
            conn, work_id, chapter_no=0, chapter_version_id=None, story_day=payload.start_day,
            event_type="PHASE_RULES_UPDATED", entity_type="world", entity_id=f"phase:{phase_key}",
            before={}, after=payload.model_dump(), evidence="作者更新故事阶段规则。", risk_level="medium",
        )
        rebuild_work_state(conn, work_id)
    return _work_or_404(work_id)


@app.put("/api/works/{work_id}/factions/{faction_name}")
def upsert_faction(work_id: str, faction_name: str, payload: FactionUpdate):
    work = _work_or_404(work_id)
    if faction_name != payload.name:
        raise HTTPException(status_code=422, detail="路径中的势力名称必须与请求一致")
    now = now_iso()
    with transaction() as conn:
        conn.execute(
            """INSERT INTO factions(id,work_id,name,precursor_name,lifecycle,formed_day,first_appearance_chapter,description,state_json,created_at,updated_at,prepared_day,public_day,active_from_day,dissolved_day)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(work_id,name) DO UPDATE SET precursor_name=excluded.precursor_name,lifecycle=excluded.lifecycle,
                 formed_day=excluded.formed_day,first_appearance_chapter=excluded.first_appearance_chapter,
                 description=excluded.description,state_json=excluded.state_json,prepared_day=excluded.prepared_day,
                 public_day=excluded.public_day,active_from_day=excluded.active_from_day,dissolved_day=excluded.dissolved_day,updated_at=excluded.updated_at""",
            (str(uuid4()), work_id, payload.name, payload.precursor_name, payload.lifecycle, payload.formed_day,
             payload.first_appearance_chapter, payload.description, json_dumps(payload.state), now, now,
             payload.prepared_day, payload.public_day, payload.active_from_day, payload.dissolved_day),
        )
        record_event(
            conn, work_id, chapter_no=0, chapter_version_id=None, story_day=payload.formed_day,
            event_type="FACTION_LIFECYCLE_UPDATED", entity_type="faction", entity_id=faction_name,
            before={}, after=payload.model_dump(), evidence="作者更新势力生命周期。", risk_level="medium",
        )
        rebuild_work_state(conn, work_id)
    return _work_or_404(work_id)


@app.post("/api/works/{work_id}/goals")
def create_story_goal(work_id: str, payload: StoryGoalCreate):
    _work_or_404(work_id)
    now = now_iso()
    with transaction() as conn:
        goal_id = str(uuid4())
        event = record_event(
            conn, work_id, chapter_no=0, chapter_version_id=None, story_day=payload.started_day,
            event_type="TASK_CREATED", entity_type="goal", entity_id=goal_id, before={},
            after={"title": payload.title, "status": payload.status, "priority": payload.priority, "details": payload.details},
            evidence="作者创建任务。", risk_level="low",
        )
        conn.execute(
            """INSERT INTO story_goals(id,work_id,owner_type,owner_id,title,status,priority,started_day,ended_day,details_json,progress_json,source_event_id,start_event_id,end_event_id,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (goal_id, work_id, payload.owner_type, payload.owner_id, payload.title, payload.status,
             payload.priority, payload.started_day, payload.ended_day, json_dumps(payload.details), json_dumps({}),
             event["id"], event["id"] if payload.status in {"active", "in_progress"} else None,
             event["id"] if payload.status in {"completed", "failed"} else None, now, now),
        )
        rebuild_work_state(conn, work_id)
    return _work_or_404(work_id)


@app.patch("/api/works/{work_id}/goals/{goal_id}")
def update_story_goal(work_id: str, goal_id: str, payload: StoryGoalUpdate):
    _work_or_404(work_id)
    changes = payload.model_dump(exclude_none=True)
    with transaction() as conn:
        current = conn.execute("SELECT * FROM story_goals WHERE id=? AND work_id=?", (goal_id, work_id)).fetchone()
        if not current:
            raise HTTPException(status_code=404, detail="任务不存在")
        before = {
            "status": current["status"], "priority": current["priority"],
            "started_day": current["started_day"], "ended_day": current["ended_day"],
            "details": json.loads(current["details_json"]),
            "progress": json.loads(current["progress_json"]),
        }
        after = {**before, **{key: value for key, value in changes.items() if key in before}}
        event = record_event(
            conn, work_id, chapter_no=int(changes.pop("chapter_no", 0)), chapter_version_id=None,
            story_day=changes.pop("story_day", None), event_type="TASK_STATE_CHANGED", entity_type="goal", entity_id=goal_id,
            before=before, after=after, evidence=str(changes.pop("evidence", "") or "作者更新任务状态。"), risk_level="medium",
        )
        status = after["status"]
        conn.execute(
            """UPDATE story_goals SET status=?,priority=?,started_day=?,ended_day=?,details_json=?,progress_json=?,source_event_id=?,
               start_event_id=CASE WHEN ? IN ('active','in_progress') THEN ? ELSE start_event_id END,
               end_event_id=CASE WHEN ? IN ('completed','failed') THEN ? ELSE end_event_id END,updated_at=? WHERE id=?""",
            (status, after["priority"], after["started_day"], after["ended_day"], json_dumps(after["details"]),
             json_dumps(after["progress"]), event["id"], status, event["id"], status, event["id"], now_iso(), goal_id),
        )
        rebuild_work_state(conn, work_id)
    return _work_or_404(work_id)


@app.post("/api/works/{work_id}/long-term-facts")
def upsert_long_term_fact(work_id: str, payload: LongTermFactUpsert):
    _work_or_404(work_id)
    now = now_iso()
    with transaction() as conn:
        current = conn.execute(
            "SELECT * FROM long_term_facts WHERE work_id=? AND entity_type=? AND entity_id IS ? AND fact_key=?",
            (work_id, payload.entity_type, payload.entity_id, payload.fact_key),
        ).fetchone()
        before = json.loads(current["value_json"]) if current else {}
        event = record_event(
            conn, work_id, chapter_no=0, chapter_version_id=None, story_day=None,
            event_type="LONG_TERM_FACT_UPDATED", entity_type="world",
            entity_id=f"{payload.entity_type}:{payload.entity_id or ''}:{payload.fact_key}", before=before, after=payload.value,
            evidence="作者更新长期事实。", risk_level="medium",
        )
        if current:
            conn.execute(
                "UPDATE long_term_facts SET value_json=?,source=?,locked=?,updated_at=? WHERE id=?",
                (json_dumps(payload.value), payload.source, int(payload.locked), now, current["id"]),
            )
        else:
            conn.execute(
                "INSERT INTO long_term_facts(id,work_id,entity_type,entity_id,fact_key,value_json,source,locked,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (str(uuid4()), work_id, payload.entity_type, payload.entity_id, payload.fact_key, json_dumps(payload.value), payload.source, int(payload.locked), now, now),
            )
        rebuild_work_state(conn, work_id)
    return {"event": event, "work": _work_or_404(work_id)}


@app.post("/api/works/{work_id}/future-plans")
def create_future_plan(work_id: str, payload: FuturePlanCreate):
    _work_or_404(work_id)
    plan_id, now = str(uuid4()), now_iso()
    with transaction() as conn:
        conn.execute(
            "INSERT INTO future_plans(id,work_id,entity_type,entity_id,plan_type,target_chapter,content_json,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (plan_id, work_id, payload.entity_type, payload.entity_id, payload.plan_type, payload.target_chapter,
             json_dumps(payload.content), payload.status, now, now),
        )
    return _work_or_404(work_id)


@app.patch("/api/works/{work_id}/future-plans/{plan_id}")
def update_future_plan(work_id: str, plan_id: str, payload: FuturePlanUpdate):
    _work_or_404(work_id)
    updates = payload.model_dump(exclude_none=True)
    with transaction() as conn:
        current = conn.execute("SELECT * FROM future_plans WHERE id=? AND work_id=?", (plan_id, work_id)).fetchone()
        if not current:
            raise HTTPException(status_code=404, detail="未来计划不存在")
        if not updates:
            return _work_or_404(work_id)
        fields = {key: json_dumps(value) if key == "content" else value for key, value in updates.items()}
        assignments = ", ".join(f"{key}=?" for key in fields)
        conn.execute(f"UPDATE future_plans SET {assignments},updated_at=? WHERE id=?", (*fields.values(), now_iso(), plan_id))
    return _work_or_404(work_id)


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
    profile = profile_for_task(resolve_profile(work_id=work_id), "state_extraction")
    extraction = enqueue_state_extraction(updated_work, chapter, "manual", profile=profile)
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
    profile = profile_for_task(resolve_profile(work_id=work_id), "state_extraction")
    return enqueue_state_extraction(work, chapter, "manual-rerun", profile=profile)


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


@app.post("/api/works/{work_id}/chapters/{chapter_no}/confirm")
def confirm_chapter(work_id: str, chapter_no: int, payload: ChapterConfirmRequest):
    work = _work_or_404(work_id)
    if not any(int(item.get("chapter_no") or 0) == chapter_no for item in work.get("chapters", [])):
        raise HTTPException(status_code=404, detail="章节不存在")
    try:
        result = review_extraction(work_id, payload.extraction_id, [item.model_dump() for item in payload.items])
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="状态提取记录不存在")
    extraction_chapter = next((item for item in work.get("chapters", []) if int(item.get("chapter_no") or 0) == chapter_no), None)
    if result.get("status") == "applied" and extraction_chapter:
        with transaction() as conn:
            conn.execute(
                "UPDATE chapters SET status='approved',state_status='confirmed',updated_at=? WHERE work_id=? AND chapter_no=?",
                (now_iso(), work_id, chapter_no),
            )
    return {"chapter_no": chapter_no, "extraction": result, "work": _work_or_404(work_id)}


@app.get("/api/works/{work_id}/chapter-plans/{chapter_no}/history")
def chapter_plan_history(work_id: str, chapter_no: int):
    _work_or_404(work_id)
    return {"items": list_chapter_plan_history(work_id, chapter_no)}


@app.get("/api/works/{work_id}/outline-versions")
def outline_version_history(work_id: str):
    _work_or_404(work_id)
    return {"items": list_outline_versions(work_id)}


@app.get("/api/works/{work_id}/contexts/chapter")
def preview_chapter_context(work_id: str, chapter_no: int):
    work = _work_or_404(work_id)
    return {
        "context": build_chapter_generation_context(work, chapter_no),
        "last_generation": latest_context_audit(work_id, chapter_no, "chapter"),
    }


@app.get("/api/works/{work_id}/contexts/outline")
def preview_outline_context(work_id: str, chapter_no: int = 1):
    work = _work_or_404(work_id)
    return {
        "context": {
            "before_chapter": chapter_no,
            "canonical_state": get_story_state(work_id, before_chapter=chapter_no),
            "long_term_rules": work.get("story_bible") or {},
            "long_term_facts": work.get("long_term_facts") or [],
            "future_plans": work.get("future_plans") or [],
            "phases": work.get("story_phases") or [],
            "factions": work.get("factions") or [],
            "goals": work.get("goals") or [],
        },
        "note": "未来规划仅作为大纲约束；正文上下文预览会明确排除它们。",
    }


@app.post("/api/works/{work_id}/story-events/{event_id}/rollback")
def rollback_story_event(work_id: str, event_id: str, payload: EventRollbackRequest):
    _work_or_404(work_id)
    try:
        result = rollback_event(work_id, event_id, payload.reason)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="故事事件不存在")
    return {**result, "work": _work_or_404(work_id)}


@app.get("/api/works/{work_id}/outline/export", response_class=PlainTextResponse)
def export_outline(work_id: str):
    _work_or_404(work_id)
    return PlainTextResponse(
        export_complete_outline(work_id),
        headers={"Content-Disposition": "attachment; filename=complete-outline.md"},
    )
