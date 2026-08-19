"""Persistent database-backed generation jobs for the single-server deployment."""

from typing import Any
from uuid import uuid4

from app.db import transaction
from app.services.novel_engine import engine
from app.services.model_profiles import resolve_profile
from app.services.quality import quality_check
from app.services.repository import get_work, save_chapter, save_outline, save_quality_report, save_story_setup
from app.services.state_extraction import extract_and_persist
from app.utils import json_dumps, json_loads, now_iso

JOB_KINDS = {"setup", "outline", "chapter", "state_extraction"}
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


def get_job(job_id: str, work_id: str | None = None) -> dict[str, Any] | None:
    with transaction() as conn:
        if work_id:
            row = conn.execute("SELECT * FROM generation_jobs WHERE id=? AND work_id=?", (job_id, work_id)).fetchone()
        else:
            row = conn.execute("SELECT * FROM generation_jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            return None
        result = dict(row)
        result["input"] = json_loads(result.pop("input_json"), {})
        result["output"] = json_loads(result.pop("output_json"), {})
        return result


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
            item = dict(row)
            item["input"] = json_loads(item.pop("input_json"), {})
            item["output"] = json_loads(item.pop("output_json"), {})
            result.append(item)
        return result


def enqueue_job(work_id: str, kind: str, payload: dict[str, Any], idempotency_key: str | None = None, model_profile_id: str | None = None) -> dict[str, Any]:
    if kind not in JOB_KINDS:
        raise ValueError(f"不支持的任务类型：{kind}")
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
            INSERT INTO generation_jobs(id, work_id, kind, input_json, status, stage, stage_label, created_at, updated_at, idempotency_key, model_profile_id)
            VALUES (?, ?, ?, ?, 'queued', 'queued', '排队中', ?, ?, ?, ?)
            """,
            (job_id, work_id, kind, json_dumps(payload), now_iso(), now_iso(), idempotency_key, model_profile_id),
        )
    return get_job(job_id, work_id)


def claim_next_job() -> dict[str, Any] | None:
    with transaction() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM generation_jobs WHERE status='queued' ORDER BY created_at, id LIMIT 1"
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
        return result


def retry_job(job_id: str, work_id: str) -> dict[str, Any] | None:
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
            "UPDATE generation_jobs SET status='queued', stage='queued', stage_label='排队中', progress=0, error='', message='', started_at=NULL, completed_at=NULL, cancel_requested_at=NULL, updated_at=? WHERE id=?",
            (now_iso(), job_id),
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
    return True


def cancel_job(job_id: str, work_id: str) -> dict[str, Any] | None:
    with transaction() as conn:
        row = conn.execute("SELECT status FROM generation_jobs WHERE id=? AND work_id=?", (job_id, work_id)).fetchone()
        if not row:
            return None
        if row["status"] == "queued":
            conn.execute("UPDATE generation_jobs SET status='canceled', stage='completed', stage_label='已取消', message='任务在排队时取消', completed_at=?, updated_at=? WHERE id=?", (now_iso(), now_iso(), job_id))
        elif row["status"] == "running":
            conn.execute("UPDATE generation_jobs SET status='cancel_requested', message='已请求取消，将在当前步骤结束后停止', cancel_requested_at=?, updated_at=? WHERE id=?", (now_iso(), now_iso(), job_id))
        elif row["status"] not in {"cancel_requested"}:
            raise ValueError("只有排队中或运行中的任务可以取消")
    return get_job(job_id, work_id)


def _complete(job_id: str, output: dict[str, Any]) -> None:
    with transaction() as conn:
        conn.execute(
            "UPDATE generation_jobs SET status='completed', stage='completed', stage_label='完成', progress=100, output_json=?, completed_at=?, updated_at=?, error='' WHERE id=?",
            (json_dumps(output), now_iso(), now_iso(), job_id),
        )


def _fail(job_id: str, error: str) -> None:
    with transaction() as conn:
        conn.execute(
            "UPDATE generation_jobs SET status='failed', stage='completed', stage_label='失败', message=?, error=?, completed_at=?, updated_at=? WHERE id=?",
            (error[:4000], error[:4000], now_iso(), now_iso(), job_id),
        )


def run_job(job: dict[str, Any]) -> None:
    """Execute one claimed job. The worker process is the only caller."""
    try:
        work = get_work(job["work_id"])
        if not work:
            raise ValueError("作品不存在")
        payload = job.get("input", {})
        kind = job["kind"]
        profile = resolve_profile(job.get("model_profile_id") or payload.get("model_profile_id"), job["work_id"])
        if is_cancel_requested(job["id"]):
            with transaction() as conn:
                conn.execute("UPDATE generation_jobs SET status='canceled', stage='completed', stage_label='已取消', completed_at=?, updated_at=? WHERE id=?", (now_iso(), now_iso(), job["id"]))
            return
        update_stage(job["id"], "generating", "正在生成结构化内容")
        if kind == "setup":
            data = engine.generate_setup(work, profile)
            if _cancel_if_requested(job["id"]):
                return
            update_stage(job["id"], "validating", "正在检查故事方案结构")
            with transaction() as conn:
                save_story_setup(conn, work["id"], data)
                _record_generation(conn, work["id"], "story_setup", payload, data)
                conn.execute("UPDATE works SET status='planning', updated_at=? WHERE id=?", (now_iso(), work["id"]))
            output = {"kind": "story_setup", "data": data}
        elif kind == "outline":
            chapter_count = int(payload.get("chapter_count", 12))
            data = engine.generate_outline(work, chapter_count, profile)
            if _cancel_if_requested(job["id"]):
                return
            update_stage(job["id"], "validating", "正在检查章节数量和结构")
            with transaction() as conn:
                save_outline(conn, work["id"], data.get("chapters", []))
                _record_generation(conn, work["id"], "outline", payload, data)
                conn.execute("UPDATE works SET status='planning', updated_at=? WHERE id=?", (now_iso(), work["id"]))
            output = {"kind": "outline", "data": data}
        elif kind == "chapter":
            chapter_no = int(payload.get("chapter_no", 1))
            data = engine.generate_chapter(work, chapter_no, payload.get("mode", "chapter"), payload.get("instruction", ""), profile)
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
            extraction = extract_and_persist(updated_work, chapter, "generation", profile=profile)
            output = {"kind": "chapter", "data": data, "quality": {"score": score, "issues": issues}, "state_extraction": extraction}
        elif kind == "state_extraction":
            chapter_no = int(payload["chapter_no"])
            chapter = next((item for item in work.get("chapters", []) if item.get("chapter_no") == chapter_no), None)
            if not chapter:
                raise ValueError("章节不存在")
            output = extract_and_persist(work, chapter, "job", force=bool(payload.get("force")), profile=profile)
        else:
            raise ValueError(f"不支持的任务类型：{kind}")
        if is_cancel_requested(job["id"]):
            with transaction() as conn:
                conn.execute("UPDATE generation_jobs SET status='canceled', stage='completed', stage_label='已取消', completed_at=?, updated_at=? WHERE id=?", (now_iso(), now_iso(), job["id"]))
            return
        update_stage(job["id"], "saving", "正在保存生成结果")
        _complete(job["id"], output)
    except Exception as exc:  # noqa: BLE001 - persisted for the UI and retry flow
        _fail(job["id"], str(exc))


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
