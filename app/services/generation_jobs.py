"""Persistent database-backed generation jobs for the single-server deployment."""

from datetime import datetime, timezone

from typing import Any
from uuid import uuid4

from app.db import transaction
from app.services.novel_engine import GenerationCancelled, engine
from app.services.model_call_logs import (
    finalize_generation_job_adoption,
    reset_model_call_context,
    set_model_call_context,
    update_model_call_status,
)
from app.services.model_profiles import profile_for_task, resolve_profile
from app.services.planning_quality import character_batch_checks, planning_checks
from app.services.planning_repository import confirmed_context, planning_context_for_step, upsert_artifact
from app.services.quality import quality_check
from app.services.repository import ensure_long_form_structure, get_work, save_chapter, save_outline, save_quality_report, save_story_setup
from app.services.story_state import validate_chapter_generation, validate_chapter_plan
from app.services.state_extraction import extract_and_persist, queue_pending_extraction
from app.services.outline_engine import ensure_lifecycle_candidates, generate_outline_batches
from app.utils import json_dumps, json_loads, now_iso

JOB_KINDS = {"setup", "outline", "volume_outline", "chapter", "state_extraction", "planning_step", "planning_character_batch"}
ACTIVE_STATUSES = {"queued", "running", "cancel_requested"}
STAGES = {
    "queued": (0, "排队中"),
    "context": (12, "准备作品上下文"),
    "generating": (50, "调用模型生成"),
    "validating": (76, "校验和修复结构"),
    "saving": (92, "保存故事资产"),
    "completed": (100, "完成"),
}


def _row(row) -> dict[str, Any] | None:
    return dict(row) if row else None


def _planning_context(work: dict[str, Any], step: str | None = None, item_key: str | None = None) -> dict[str, Any]:
    """Merge confirmed author decisions with an abstract inspiration brief.

    The brief deliberately excludes provenance titles and other source surface
    details.  It can guide market fit and pacing while the planning prompts still
    generate a genuinely new story world, cast and event chain.
    """
    context = (
        planning_context_for_step(work["id"], step, item_key)
        if step
        else confirmed_context(work["id"])
    )
    blueprint = work.get("inspiration_blueprint") or {}
    content = blueprint.get("content") if isinstance(blueprint, dict) else {}
    brief = content.get("creative_brief") if isinstance(content, dict) else None
    if isinstance(brief, dict) and step != "summary":
        context["inspiration_brief"] = brief
        context["originality_requirements"] = (
            (blueprint.get("originality") or {}).get("checks", [])
            if isinstance(blueprint, dict) else []
        )
    return context


def _job_result(row) -> dict[str, Any]:
    result = dict(row)
    result["input"] = json_loads(result.pop("input_json"), {})
    result["output"] = json_loads(result.pop("output_json"), {})
    result["metrics"] = json_loads(result.pop("metrics_json", "{}"), {})
    return result


def _generation_meta() -> dict[str, Any]:
    meta = engine.generation_metadata()
    return {
        "generation_source": str(meta.get("generation_source") or "fallback"),
        "repair_attempts": int(meta.get("repair_attempts") or 0),
        "parse_status": str(meta.get("parse_status") or "not_checked"),
        "quality_status": str(meta.get("quality_status") or "not_checked"),
    }


def get_job(job_id: str, work_id: str | None = None) -> dict[str, Any] | None:
    with transaction() as conn:
        if work_id:
            row = conn.execute("SELECT * FROM generation_jobs WHERE id=? AND work_id=?", (job_id, work_id)).fetchone()
        else:
            row = conn.execute("SELECT * FROM generation_jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            return None
        return _job_result(row)


def list_jobs(work_id: str, active_only: bool = False) -> list[dict[str, Any]]:
    with transaction() as conn:
        query = "SELECT * FROM generation_jobs WHERE work_id=?"
        params: list[Any] = [work_id]
        if active_only:
            query += " AND status IN ('queued','running','cancel_requested')"
        query += " ORDER BY created_at DESC LIMIT 30"
        rows = conn.execute(query, params).fetchall()
        result = []
        for row in rows:
            result.append(_job_result(row))
        return result


def recover_interrupted_jobs(stale_after_seconds: int = 900) -> dict[str, int]:
    """Finalize cancel requests and fail genuinely stale running jobs after a worker restart."""
    canceled = 0
    failed = 0
    now = datetime.now(timezone.utc)
    with transaction() as conn:
        cancel_rows = conn.execute(
            "SELECT id FROM generation_jobs WHERE status='cancel_requested'"
        ).fetchall()
        for row in cancel_rows:
            conn.execute(
                """
                UPDATE generation_jobs
                SET status='canceled', stage='completed', stage_label='已取消', progress=100,
                    message='worker 重启后已完成取消', completed_at=?, updated_at=?
                WHERE id=? AND status='cancel_requested'
                """,
                (now_iso(), now_iso(), row["id"]),
            )
            canceled += 1

        running_rows = conn.execute(
            "SELECT id, started_at FROM generation_jobs WHERE status='running'"
        ).fetchall()
        for row in running_rows:
            try:
                started_at = datetime.fromisoformat(str(row["started_at"] or ""))
                if started_at.tzinfo is None:
                    started_at = started_at.replace(tzinfo=timezone.utc)
            except ValueError:
                started_at = datetime.min.replace(tzinfo=timezone.utc)
            if (now - started_at).total_seconds() < stale_after_seconds:
                continue
            message = "worker 中断，任务未能完成；可重新生成"
            conn.execute(
                """
                UPDATE generation_jobs
                SET status='failed', stage='completed', stage_label='中断',
                    message=?, error=?, completed_at=?, updated_at=?
                WHERE id=? AND status='running'
                """,
                (message, message, now_iso(), now_iso(), row["id"]),
            )
            failed += 1
    return {"canceled": canceled, "failed": failed}


def enqueue_job(work_id: str, kind: str, payload: dict[str, Any], idempotency_key: str | None = None, model_profile_id: str | None = None) -> dict[str, Any]:
    if kind not in JOB_KINDS:
        raise ValueError(f"不支持的任务类型：{kind}")
    work = get_work(work_id)
    if not work:
        raise ValueError("作品不存在")
    requested_profile_id = model_profile_id or work.get("model_profile_id")
    profile = resolve_profile(
        requested_profile_id,
        work_id,
        require_requested_profile=bool(requested_profile_id),
    )
    resolved_profile_id = (profile or {}).get("id") or None
    resolved_provider = str((profile or {}).get("provider") or "fallback")
    resolved_model = str((profile or {}).get("model") or "本地演示生成")
    resolved_base_url = str((profile or {}).get("base_url") or "")
    with transaction() as conn:
        if idempotency_key:
            existing = conn.execute(
                "SELECT id FROM generation_jobs WHERE work_id=? AND idempotency_key=? LIMIT 1",
                (work_id, idempotency_key),
            ).fetchone()
            if existing:
                return get_job(existing["id"], work_id)
        job_id = str(uuid4())
        conn.execute(
            """
            INSERT INTO generation_jobs(
                id, work_id, kind, input_json, status, stage, stage_label, created_at, updated_at,
                idempotency_key, model_profile_id, resolved_provider, resolved_model, resolved_base_url, metrics_json
            ) VALUES (?, ?, ?, ?, 'queued', 'queued', '排队中', ?, ?, ?, ?, ?, ?, ?, '{}')
            """,
            (
                job_id, work_id, kind, json_dumps(payload), now_iso(), now_iso(), idempotency_key,
                resolved_profile_id, resolved_provider, resolved_model, resolved_base_url,
            ),
        )
    return get_job(job_id, work_id)


def enqueue_state_extraction(
    work: dict[str, Any],
    chapter: dict[str, Any],
    source: str = "generation",
    profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Queue extraction separately so chapter generation can return immediately."""
    pending = queue_pending_extraction(work, chapter, source, profile)
    idempotency_suffix = str(uuid4()) if source == "manual-rerun" else source
    extraction_job = enqueue_job(
        work["id"],
        "state_extraction",
        {"chapter_no": int(chapter.get("chapter_no") or 0), "force": True, "source": source},
        idempotency_key=f"state-extraction:{pending.get('chapter_version_id')}:{idempotency_suffix}",
        model_profile_id=(profile or {}).get("id"),
    )
    # An existing reviewed extraction may be returned for this chapter version.
    # The response must still describe the newly queued rerun so the UI polls it.
    pending = dict(pending)
    pending["status"] = "queued"
    pending["warning"] = "状态提取任务已排队，完成后可审核结果。"
    pending["job_id"] = extraction_job["id"]
    return pending


def claim_next_job() -> dict[str, Any] | None:
    with transaction() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT queued.* FROM generation_jobs AS queued
            WHERE queued.status='queued'
              AND NOT EXISTS (
                  SELECT 1 FROM generation_jobs AS active
                  WHERE active.work_id=queued.work_id
                    AND active.status IN ('running', 'cancel_requested')
              )
            ORDER BY CASE WHEN queued.kind = 'state_extraction' THEN 1 ELSE 0 END,
                     queued.created_at, queued.id
            LIMIT 1
            """
        ).fetchone()
        if not row:
            return None
        now = now_iso()
        conn.execute(
            "UPDATE generation_jobs SET status='running', stage='context', stage_label='准备作品上下文', progress=12, attempts=attempts+1, started_at=?, updated_at=? WHERE id=?",
            (now, now, row["id"]),
        )
        result = dict(row)
        result["status"] = "running"
        result["started_at"] = now
        result["input"] = json_loads(result.pop("input_json"), {})
        result["output"] = {}
        result["metrics"] = json_loads(result.pop("metrics_json", "{}"), {})
        return result


def retry_job(job_id: str, work_id: str) -> dict[str, Any] | None:
    work = get_work(work_id)
    if not work:
        return None
    requested_profile_id = work.get("model_profile_id")
    profile = resolve_profile(
        requested_profile_id,
        work_id,
        require_requested_profile=bool(requested_profile_id),
    )
    with transaction() as conn:
        row = conn.execute(
            "SELECT status FROM generation_jobs WHERE id=? AND work_id=? LIMIT 1",
            (job_id, work_id),
        ).fetchone()
        if not row:
            return None
        if row["status"] != "failed":
            raise ValueError("只有失败任务可以重试")
        conn.execute(
            """
            UPDATE generation_jobs
            SET status='queued', stage='queued', stage_label='排队中', progress=0, error='', message='',
                started_at=NULL, model_started_at=NULL, model_first_output_at=NULL, completed_at=NULL, cancel_requested_at=NULL,
                input_tokens=NULL, output_tokens=NULL, total_tokens=NULL, metrics_json='{}',
                model_profile_id=?, resolved_provider=?, resolved_model=?, resolved_base_url=?, updated_at=?
            WHERE id=?
            """,
            (
                (profile or {}).get("id"),
                str((profile or {}).get("provider") or "fallback"),
                str((profile or {}).get("model") or "本地演示生成"),
                str((profile or {}).get("base_url") or ""),
                now_iso(),
                job_id,
            ),
        )
    return get_job(job_id, work_id)


def _record_generation(conn, work_id: str, kind: str, input_data: dict[str, Any], output_data: dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO generation_runs(id, work_id, kind, input_json, output_json, status, created_at) VALUES (?, ?, ?, ?, ?, 'completed', ?)",
        (str(uuid4()), work_id, kind, json_dumps(input_data), json_dumps(output_data), now_iso()),
    )


def update_stage(job_id: str, stage: str, message: str = "") -> None:
    progress, label = STAGES.get(stage, (0, stage))
    with transaction() as conn:
        conn.execute("UPDATE generation_jobs SET stage=?, stage_label=?, progress=?, message=?, updated_at=? WHERE id=? AND status='running'",
                     (stage, label, progress, message, now_iso(), job_id))


def mark_model_started(job_id: str, profile: dict[str, Any] | None) -> None:
    now = now_iso()
    with transaction() as conn:
        conn.execute(
            """
            UPDATE generation_jobs
            SET stage='generating', stage_label='模型生成中', progress=30,
                message=?, model_started_at=?, updated_at=?
            WHERE id=? AND status='running'
            """,
            (
                f"正在调用 {str((profile or {}).get('provider') or '本地生成')} / {str((profile or {}).get('model') or 'fallback')}",
                now,
                now,
                job_id,
            ),
        )


def update_model_heartbeat(job_id: str, message: str) -> None:
    with transaction() as conn:
        now = now_iso()
        conn.execute(
            """
            UPDATE generation_jobs
            SET message=?,
                stage_label=CASE
                    WHEN ? LIKE '模型正在流式输出%' THEN '流式生成中'
                    WHEN ? LIKE '模型正在思考%' THEN '模型思考中'
                    WHEN ? LIKE 'Codex 正在生成%' THEN 'Codex 生成中'
                    WHEN ? LIKE '当前模型不支持流式输出%' THEN '普通生成中'
                    ELSE stage_label
                END,
                progress=CASE
                    WHEN ? LIKE '模型输出完成%' THEN 72
                    WHEN ? LIKE '模型正在流式输出%' THEN 55
                    WHEN ? LIKE '模型正在思考%' THEN 42
                    WHEN ? LIKE 'Codex 正在生成%' THEN 45
                    WHEN ? LIKE '当前模型不支持流式输出%' THEN 40
                    WHEN ? LIKE '已发送请求%' THEN 35
                    ELSE progress
                END,
                model_first_output_at=CASE
                    WHEN model_first_output_at IS NULL AND ? LIKE '模型正在流式输出%' THEN ?
                    ELSE model_first_output_at
                END,
                updated_at=?
            WHERE id=? AND status='running' AND stage='generating'
            """,
            (
                message[:500],
                message, message, message, message,
                message, message, message, message, message, message,
                message, now, now, job_id,
            ),
        )


def update_character_batch_progress(job_id: str, completed: int, total: int) -> None:
    """Expose useful progress while a single model response is split into drafts."""
    total = max(1, total)
    progress = 76 + int(16 * min(completed, total) / total)
    with transaction() as conn:
        conn.execute(
            """
            UPDATE generation_jobs
            SET stage='validating', stage_label='正在整理人物小传', progress=?,
                message=?, updated_at=?
            WHERE id=? AND status='running'
            """,
            (progress, f"正在保存人物小传 {completed}/{total}", now_iso(), job_id),
        )


def _split_usage(usage: dict[str, Any], count: int) -> list[dict[str, int]]:
    """Keep planning-session token totals accurate when one response creates many drafts."""
    if count <= 0 or any(usage.get(key) is None for key in ("input_tokens", "output_tokens", "total_tokens")):
        return [{} for _ in range(max(0, count))]
    result = [{} for _ in range(count)]
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        value = int(usage[key])
        quotient, remainder = divmod(value, count)
        for index in range(count):
            result[index][key] = quotient + (1 if index < remainder else 0)
    return result


def _merge_usage(*usages: dict[str, Any]) -> dict[str, int]:
    """Combine the original batch request with a targeted repair request."""
    result: dict[str, int] = {}
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        values = [usage.get(key) for usage in usages if usage.get(key) is not None]
        if values:
            result[key] = sum(int(value) for value in values)
    return result


def is_cancel_requested(job_id: str) -> bool:
    with transaction() as conn:
        row = conn.execute("SELECT status FROM generation_jobs WHERE id=?", (job_id,)).fetchone()
        return bool(row and row["status"] == "cancel_requested")


def _cancel_if_requested(job_id: str) -> bool:
    """Atomically finish a cancellation request before persisting model output."""
    if not is_cancel_requested(job_id):
        return False
    with transaction() as conn:
        conn.execute(
            "UPDATE generation_jobs SET status='canceled', stage='completed', stage_label='已取消', message='已取消，未保存本次生成结果', completed_at=?, updated_at=? WHERE id=? AND status='cancel_requested'",
            (now_iso(), now_iso(), job_id),
        )
    finalize_generation_job_adoption(job_id)
    return True


def cancel_job(job_id: str, work_id: str) -> dict[str, Any] | None:
    with transaction() as conn:
        row = conn.execute("SELECT status FROM generation_jobs WHERE id=? AND work_id=?", (job_id, work_id)).fetchone()
        if not row:
            return None
        if row["status"] == "queued":
            conn.execute("UPDATE generation_jobs SET status='canceled', stage='completed', stage_label='已取消', message='任务在排队时取消', completed_at=?, updated_at=? WHERE id=?", (now_iso(), now_iso(), job_id))
        elif row["status"] == "running":
            conn.execute("UPDATE generation_jobs SET status='cancel_requested', message='正在终止模型调用…', cancel_requested_at=?, updated_at=? WHERE id=?", (now_iso(), now_iso(), job_id))
        elif row["status"] not in {"cancel_requested"}:
            raise ValueError("只有排队中或运行中的任务可以取消")
    return get_job(job_id, work_id)


def _complete(
    job_id: str,
    output: dict[str, Any],
    usage: dict[str, int] | None = None,
    *,
    adopted_call_id: str | None = None,
) -> None:
    usage = usage or {}
    finalize_generation_job_adoption(job_id, adopted_call_id)
    with transaction() as conn:
        row = conn.execute("SELECT created_at, started_at, model_started_at, model_first_output_at FROM generation_jobs WHERE id=?", (job_id,)).fetchone()
        finished_at = now_iso()
        metrics: dict[str, Any] = {}
        if row:
            def elapsed_ms(start: str | None, end: str) -> int | None:
                if not start:
                    return None
                try:
                    return max(0, int((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds() * 1000))
                except ValueError:
                    return None
            metrics = {
                "queue_ms": elapsed_ms(row["created_at"], row["started_at"]),
                "run_ms": elapsed_ms(row["started_at"], finished_at),
                "model_ms": elapsed_ms(row["model_started_at"], finished_at),
                "first_output_ms": elapsed_ms(row["model_started_at"], row["model_first_output_at"]) if row["model_first_output_at"] else None,
                "output_stream_ms": elapsed_ms(row["model_first_output_at"], finished_at) if row["model_first_output_at"] else None,
            }
        conn.execute(
            """
            UPDATE generation_jobs SET status='completed', stage='completed', stage_label='完成', progress=100,
                output_json=?, input_tokens=?, output_tokens=?, total_tokens=?, metrics_json=?, completed_at=?, updated_at=?, error=''
            WHERE id=?
            """,
            (
                json_dumps(output), usage.get("input_tokens"), usage.get("output_tokens"), usage.get("total_tokens"),
                json_dumps(metrics), finished_at, finished_at, job_id,
            ),
        )


def _fail(job_id: str, error: str) -> None:
    finalize_generation_job_adoption(job_id)
    with transaction() as conn:
        row = conn.execute("SELECT created_at, started_at, model_started_at, model_first_output_at FROM generation_jobs WHERE id=?", (job_id,)).fetchone()
        finished_at = now_iso()
        metrics: dict[str, Any] = {}
        if row:
            def elapsed_ms(start: str | None, end: str) -> int | None:
                if not start:
                    return None
                try:
                    return max(0, int((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds() * 1000))
                except ValueError:
                    return None
            metrics = {
                "queue_ms": elapsed_ms(row["created_at"], row["started_at"]),
                "run_ms": elapsed_ms(row["started_at"], finished_at),
                "model_ms": elapsed_ms(row["model_started_at"], finished_at),
                "first_output_ms": elapsed_ms(row["model_started_at"], row["model_first_output_at"]) if row["model_first_output_at"] else None,
                "output_stream_ms": elapsed_ms(row["model_first_output_at"], finished_at) if row["model_first_output_at"] else None,
            }
        conn.execute(
            "UPDATE generation_jobs SET status='failed', stage='completed', stage_label='失败', message=?, error=?, metrics_json=?, completed_at=?, updated_at=? WHERE id=?",
            (error[:4000], error[:4000], json_dumps(metrics), finished_at, finished_at, job_id),
    )


def run_job(job: dict[str, Any]) -> None:
    """Execute one claimed job. The worker process is the only caller."""
    context_token = set_model_call_context(
        user_id="demo-user",
        work_id=job.get("work_id"),
        generation_job_id=job.get("id"),
        call_kind=str(job.get("kind") or "model"),
    )
    try:
        work = get_work(job["work_id"])
        if not work:
            raise ValueError("作品不存在")
        payload = job.get("input", {})
        kind = job["kind"]
        profile = resolve_profile(
            job.get("model_profile_id") or payload.get("model_profile_id"),
            job["work_id"],
            require_requested_profile=bool(job.get("model_profile_id") or payload.get("model_profile_id")),
        )
        if is_cancel_requested(job["id"]):
            with transaction() as conn:
                conn.execute("UPDATE generation_jobs SET status='canceled', stage='completed', stage_label='已取消', completed_at=?, updated_at=? WHERE id=?", (now_iso(), now_iso(), job["id"]))
            return
        profile = profile_for_task(profile, kind)
        mark_model_started(job["id"], profile)
        if kind == "setup":
            data = engine.generate_setup(work, profile)
            if _cancel_if_requested(job["id"]):
                return
            update_stage(job["id"], "validating", "正在检查故事方案结构")
            with transaction() as conn:
                save_story_setup(conn, work["id"], data)
                _record_generation(conn, work["id"], "story_setup", payload, data)
                conn.execute("UPDATE works SET status='planning', updated_at=? WHERE id=?", (now_iso(), work["id"]))
            output = {"kind": "story_setup", "data": data, **{
                key: data.get(key) for key in ("generation_source", "repair_attempts", "parse_status", "quality_status")
            }}
        elif kind == "outline":
            ensure_lifecycle_candidates(work["id"])
            ensure_long_form_structure(
                work["id"],
                payload.get("total_target_chapters") or payload.get("chapter_count"),
            )
            work = get_work(work["id"])
            data = generate_outline_batches(work, payload, profile)
            validation_work = {
                **work,
                "chapter_plans": [item for item in work.get("chapter_plans", []) if int(item.get("chapter_no") or 0) < data["from_chapter"]],
            }
            for item in data["chapters"]:
                errors = validate_chapter_plan(validation_work, item, replacing_no=int(item["chapter_no"]))
                if errors:
                    raise ValueError("；".join(errors))
                validation_work["chapter_plans"].append(item)
            outline_meta = _generation_meta()
            data.update(outline_meta)
            if _cancel_if_requested(job["id"]):
                return
            update_stage(job["id"], "validating", "正在检查章节数量和结构")
            with transaction() as conn:
                saved = save_outline(
                    conn, work["id"], data.get("chapters", []), mode=data["mode"],
                    from_chapter=data["from_chapter"], to_chapter=data["to_chapter"],
                    request={**payload, **({"repair_history": data["repair_history"]} if data.get("repair_history") else {})},
                    expected_outline_version=payload.get("expected_outline_version"),
                    expected_fact_version=data["fact_version"] if payload.get("expected_fact_version") is None else payload.get("expected_fact_version"),
                )
                data.update(saved)
                _record_generation(conn, work["id"], "outline", payload, data)
                conn.execute("UPDATE works SET status='planning', target_chapter_count=?, updated_at=? WHERE id=?", (data["total_target_chapters"], now_iso(), work["id"]))
            output = {"kind": "outline", "data": data, **outline_meta}
        elif kind == "volume_outline":
            # A volume draft is deliberately not persisted here.  The author reviews
            # the returned draft in the editor and explicitly saves it afterwards.
            # This also keeps existing chapter plans untouched.
            ensure_long_form_structure(work["id"])
            work = get_work(work["id"])
            volume_id = str(payload.get("volume_id") or "").strip()
            if not volume_id:
                raise ValueError("请选择需要生成卷纲的分卷")
            if not any(item.get("id") == volume_id for item in work.get("story_volumes", [])):
                raise ValueError("指定分卷不存在")
            target_stage_id = str(payload.get("target_stage_id") or "").strip() or None
            if target_stage_id and not any(
                item.get("id") == target_stage_id and item.get("volume_id") == volume_id
                for item in work.get("narrative_stages", [])
            ):
                raise ValueError("指定叙事阶段不属于当前分卷")
            data, usage, source = engine.generate_volume_outline(
                work,
                volume_id,
                target_stage_id=target_stage_id,
                instruction=str(payload.get("instruction") or ""),
                profile=profile,
                on_progress=lambda message: update_model_heartbeat(job["id"], message),
                is_cancelled=lambda: is_cancel_requested(job["id"]),
            )
            if _cancel_if_requested(job["id"]):
                return
            update_stage(job["id"], "validating", "正在标记卷纲草稿中的缺失项")
            output = {
                "kind": "volume_outline",
                "data": data,
                "usage": usage,
                "generation_source": source,
                **_generation_meta(),
            }
            job["usage"] = usage
        elif kind == "chapter":
            chapter_no = int(payload.get("chapter_no", 1))
            timeline_errors = validate_chapter_generation(work, chapter_no)
            if timeline_errors:
                raise ValueError("；".join(timeline_errors))
            data = engine.generate_chapter(
                work,
                chapter_no,
                payload.get("mode", "chapter"),
                payload.get("instruction", ""),
                profile,
                on_progress=lambda message: update_model_heartbeat(job["id"], message),
                is_cancelled=lambda: is_cancel_requested(job["id"]),
            )
            if _cancel_if_requested(job["id"]):
                return
            update_stage(job["id"], "validating", "正在执行一致性检查")
            issues, score = quality_check({**work, "chapters": [*work.get("chapters", []), data]}, chapter_no, data.get("content", ""))
            with transaction() as conn:
                save_chapter(conn, work["id"], {**data, "status": "draft"})
                save_quality_report(conn, work["id"], chapter_no, issues, score)
                _record_generation(conn, work["id"], "chapter", payload, data)
                conn.execute("UPDATE works SET status='writing', updated_at=? WHERE id=?", (now_iso(), work["id"]))
            updated_work = get_work(work["id"])
            chapter = next(item for item in updated_work["chapters"] if item["chapter_no"] == chapter_no)
            extraction = enqueue_state_extraction(updated_work, chapter, "generation", profile=profile)
            chapter_meta = _generation_meta()
            chapter_meta["quality_status"] = "passed" if not issues else "failed"
            update_model_call_status(
                engine.last_model_call_id(),
                quality_status=chapter_meta["quality_status"],
            )
            data.update(chapter_meta)
            output = {"kind": "chapter", "data": data, "quality": {"score": score, "issues": issues}, "state_extraction": extraction, **chapter_meta}
        elif kind == "state_extraction":
            chapter_no = int(payload["chapter_no"])
            chapter = next((item for item in work.get("chapters", []) if item.get("chapter_no") == chapter_no), None)
            if not chapter:
                raise ValueError("章节不存在")
            output = {**extract_and_persist(work, chapter, payload.get("source", "job"), force=bool(payload.get("force")), profile=profile), **_generation_meta()}
        elif kind == "planning_step":
            step = str(payload.get("step") or "")
            item_key = str(payload.get("item_key") or "default")
            context = _planning_context(work, step, item_key)
            data, usage, source = engine.generate_planning_step(
                work,
                step,
                item_key,
                context,
                feedback=str(payload.get("feedback") or ""),
                preset=str(payload.get("preset") or "custom"),
                candidate_count=int(payload.get("candidate_count") or (3 if step == "contract" else 1)),
                profile=profile,
                on_progress=lambda message: update_model_heartbeat(job["id"], message),
                is_cancelled=lambda: is_cancel_requested(job["id"]),
            )
            if _cancel_if_requested(job["id"]):
                return
            update_stage(job["id"], "validating", "正在检查当前步骤结构和语言风险")
            checks = planning_checks(step, data, context)
            planning_meta = _generation_meta()
            planning_meta["quality_status"] = "passed" if checks.get("ok") else "failed"
            update_model_call_status(
                engine.last_model_call_id(),
                quality_status=planning_meta["quality_status"],
            )
            upsert_artifact(
                work["id"], step, item_key, data, source=source,
                feedback=str(payload.get("feedback") or ""), checks=checks,
                usage=usage, model=str((profile or {}).get("model") or ""),
            )
            output = {"kind": "planning_step", "step": step, "item_key": item_key, "data": data, "checks": checks, "usage": usage, **planning_meta}
            job["usage"] = usage
        elif kind == "planning_character_batch":
            item_keys = list(dict.fromkeys(str(item) for item in payload.get("item_keys", []) if str(item)))
            if not item_keys:
                raise ValueError("没有需要生成的人物")
            context = _planning_context(work, "character")
            drafts, usage, source = engine.generate_character_batch(
                work,
                item_keys,
                context,
                feedback=str(payload.get("feedback") or ""),
                preset=str(payload.get("preset") or "custom"),
                profile=profile,
                on_progress=lambda message: update_model_heartbeat(job["id"], message),
                is_cancelled=lambda: is_cancel_requested(job["id"]),
            )
            if _cancel_if_requested(job["id"]):
                return
            update_stage(job["id"], "validating", "正在检查批量人物小传")
            if set(drafts) != set(item_keys) or any(not isinstance(drafts.get(item_key), dict) for item_key in item_keys):
                raise ValueError("批量人物模型结果与请求人物集合不一致，未保存任何人物草稿")
            checks_by_key = character_batch_checks(drafts, context)
            invalid_keys = [item_key for item_key in item_keys if checks_by_key[item_key]["blocking"]]
            batch_meta = _generation_meta()
            batch_meta["quality_status"] = "failed" if invalid_keys else "passed"
            update_model_call_status(
                engine.last_model_call_id(),
                quality_status=batch_meta["quality_status"],
            )
            # Only retry the failing cards once.  This retains bulk speed for the
            # normal path while preventing a whole batch from being saved with
            # copied fields or inadequate visual detail.
            if invalid_keys and source == "model":
                update_stage(job["id"], "validating", f"正在定向修复 {len(invalid_keys)} 名人物小传")
                repair_feedback = "\n".join(
                    f"{drafts[item_key].get('name') or item_key}：{'；'.join(checks_by_key[item_key]['blocking'])}"
                    for item_key in invalid_keys
                )
                repaired, repair_usage, repair_source = engine.generate_character_batch(
                    work,
                    invalid_keys,
                    context,
                    feedback=(
                        f"{str(payload.get('feedback') or '')}\n"
                        "以下是本次质量校验未通过项。仅重写对应人物，必须让 appearance、personality、voice 三栏内容独立：\n"
                        f"{repair_feedback}"
                    ).strip(),
                    preset=str(payload.get("preset") or "custom"),
                    profile=profile,
                    on_progress=lambda message: update_model_heartbeat(job["id"], message),
                    is_cancelled=lambda: is_cancel_requested(job["id"]),
                )
                if _cancel_if_requested(job["id"]):
                    return
                if repair_source == "model":
                    drafts.update(repaired)
                    usage = _merge_usage(usage, repair_usage)
                    checks_by_key = character_batch_checks(drafts, context)
                    batch_meta = _generation_meta()
                    batch_meta["repair_attempts"] = int(batch_meta.get("repair_attempts") or 0) + 1
                    batch_meta["parse_status"] = "repaired"
                    batch_meta["quality_status"] = "failed" if any(item["blocking"] for item in checks_by_key.values()) else "passed"
                    update_model_call_status(
                        engine.last_model_call_id(),
                        quality_status=batch_meta["quality_status"],
                    )
            usage_by_item = _split_usage(usage, len(item_keys))
            saved_items = []
            for index, item_key in enumerate(item_keys, start=1):
                if _cancel_if_requested(job["id"]):
                    return
                data = {"character": drafts[item_key]}
                checks = checks_by_key[item_key]
                upsert_artifact(
                    work["id"], "character", item_key, data, source=source,
                    feedback=str(payload.get("feedback") or ""), checks=checks,
                    usage=usage_by_item[index - 1], model=str((profile or {}).get("model") or ""),
                )
                saved_items.append({
                    "item_key": item_key,
                    "name": drafts[item_key].get("name", item_key),
                    "status": "generated",
                    "checks": checks,
                })
                update_character_batch_progress(job["id"], index, len(item_keys))
            output = {
                "kind": "planning_character_batch", "items": saved_items,
                "usage": usage, **batch_meta,
            }
            job["usage"] = usage
        else:
            raise ValueError(f"不支持的任务类型：{kind}")
        if is_cancel_requested(job["id"]):
            with transaction() as conn:
                conn.execute("UPDATE generation_jobs SET status='canceled', stage='completed', stage_label='已取消', completed_at=?, updated_at=? WHERE id=?", (now_iso(), now_iso(), job["id"]))
            finalize_generation_job_adoption(job["id"])
            return
        update_stage(job["id"], "saving", "正在保存生成结果")
        quality_status = str(
            output.get("quality_status")
            or ((output.get("data") or {}).get("quality_status") if isinstance(output.get("data"), dict) else "")
            or "not_checked"
        )
        adopted_call_id = (
            None
            if kind == "volume_outline" or quality_status in {"failed", "error", "quality_failed"}
            else engine.last_model_call_id()
        )
        _complete(
            job["id"],
            output,
            job.get("usage") or output.get("usage") or {},
            adopted_call_id=adopted_call_id,
        )
    except GenerationCancelled:
        finalize_generation_job_adoption(job["id"])
        with transaction() as conn:
            conn.execute(
                "UPDATE generation_jobs SET status='canceled', stage='completed', stage_label='已取消', progress=100, message='已取消，未保存本次生成结果', completed_at=?, updated_at=? WHERE id=?",
                (now_iso(), now_iso(), job["id"]),
            )
    except Exception as exc:  # noqa: BLE001 - persisted for the UI and retry flow
        _fail(job["id"], str(exc))
    finally:
        reset_model_call_context(context_token)


def run_worker_once() -> bool:
    job = claim_next_job()
    if not job:
        return False
    run_job(job)
    return True


if __name__ == "__main__":
    import time

    while True:
        if not run_worker_once():
            time.sleep(1)
