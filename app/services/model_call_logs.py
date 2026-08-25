"""Per-model-call observability records.

The existing generation_jobs table describes a product task.  This module keeps
the lower-level request/response timeline so one task can be correlated with
multiple provider calls without exposing credentials.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
from typing import Any, Iterator
from uuid import uuid4

from app.db import transaction
from app.utils import json_loads, now_iso


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ModelCallContext:
    user_id: str = "demo-user"
    work_id: str | None = None
    generation_job_id: str | None = None
    call_kind: str = "model"


_context: ContextVar[ModelCallContext] = ContextVar(
    "model_call_context", default=ModelCallContext()
)


def set_model_call_context(
    *,
    user_id: str = "demo-user",
    work_id: str | None = None,
    generation_job_id: str | None = None,
    call_kind: str = "model",
) -> Token[ModelCallContext]:
    return _context.set(ModelCallContext(user_id, work_id, generation_job_id, call_kind))


def reset_model_call_context(token: Token[ModelCallContext]) -> None:
    _context.reset(token)


@contextmanager
def model_call_context(
    *,
    user_id: str = "demo-user",
    work_id: str | None = None,
    generation_job_id: str | None = None,
    call_kind: str = "model",
) -> Iterator[None]:
    token = set_model_call_context(
        user_id=user_id,
        work_id=work_id,
        generation_job_id=generation_job_id,
        call_kind=call_kind,
    )
    try:
        yield
    finally:
        reset_model_call_context(token)


def _dump(value: Any) -> str:
    import json

    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return json.dumps(str(value), ensure_ascii=False)


def _elapsed_ms(start: str | None, end: str | None) -> int | None:
    if not start or not end:
        return None
    try:
        return max(0, int((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds() * 1000))
    except (TypeError, ValueError):
        return None


def _safe_error(error: BaseException | str | None) -> str:
    return str(error or "")[:12000]


TASK_LABELS = {
    "setup": "故事方案",
    "outline": "章节大纲",
    "volume_outline": "卷纲",
    "chapter": "正文生成",
    "state_extraction": "状态提取",
    "planning_step": "分阶段规划",
    "planning_character_batch": "批量人物小传",
}


def _dict_json(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _observability(request: dict[str, Any]) -> dict[str, Any]:
    return _dict_json(request.get("observability"))


def _task_name(kind: str | None, payload: dict[str, Any], call_kind: str | None) -> str:
    kind = str(kind or call_kind or "model")
    if kind == "planning_step":
        step = str(payload.get("step") or "").strip()
        return f"规划：{step}" if step else TASK_LABELS[kind]
    return TASK_LABELS.get(kind, kind or "模型调用")


def _failure_category(status: str, error: str, parse_status: str, quality_status: str) -> str | None:
    if status == "timeout":
        return "timeout"
    if status == "canceled":
        return "user_canceled"
    if quality_status in {"failed", "error", "quality_failed"}:
        return "quality_failure"
    if parse_status in {"failed", "invalid", "error", "structure_error"}:
        return "structure_error"
    # New records classify from the explicit call-level fields. Do not infer a
    # parse or quality failure from free-form transport error text.
    return "transport_failure" if status == "failed" else None


def _joined_call_query(where: str = "", order: str = "") -> str:
    return f"""
        SELECT c.*, w.title AS work_title,
               j.kind AS generation_kind, j.status AS generation_status,
               j.input_json AS generation_input_json, j.output_json AS generation_output_json,
               j.resolved_provider AS generation_resolved_provider,
               j.resolved_model AS generation_resolved_model,
               j.resolved_base_url AS generation_resolved_base_url
        FROM model_call_logs AS c
        LEFT JOIN works AS w ON w.id=c.work_id
        LEFT JOIN generation_jobs AS j ON j.id=c.generation_job_id
        {where}
        {order}
    """


def start_model_call(
    profile: dict[str, Any] | None,
    request: Any,
    *,
    call_kind: str | None = None,
    user_id: str | None = None,
    work_id: str | None = None,
    generation_job_id: str | None = None,
) -> str | None:
    """Insert a running call record; logging failure must never break generation."""
    context = _context.get()
    call_id = str(uuid4())
    started_at = now_iso()
    profile = profile or {}
    request_dict = request if isinstance(request, dict) else {}
    observability = _observability(request_dict)
    active_generation_job_id = generation_job_id if generation_job_id is not None else context.generation_job_id
    initial_adoption = "pending" if active_generation_job_id else "not_applicable"
    repair_parent = str(
        request_dict.get("repair_of_call_id")
        or observability.get("repair_of_call_id")
        or ""
    ) or None
    try:
        with transaction() as conn:
            conn.execute(
                """
                INSERT INTO model_call_logs(
                    id, user_id, work_id, generation_job_id, model_profile_id,
                    call_kind, provider, model, base_url, status,
                    parse_status, quality_status, adoption_status, repair_of_call_id,
                    request_json, started_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    call_id,
                    user_id or context.user_id,
                    work_id if work_id is not None else context.work_id,
                    active_generation_job_id,
                    profile.get("id"),
                    call_kind or context.call_kind,
                    str(profile.get("provider") or ""),
                    str(profile.get("model") or ""),
                    str(profile.get("base_url") or ""),
                    "not_recorded",
                    "not_recorded",
                    initial_adoption,
                    repair_parent,
                    _dump(request),
                    started_at,
                    started_at,
                ),
            )
        return call_id
    except Exception:  # noqa: BLE001 - observability must be best effort
        logger.exception("model_call_log_start_failed")
        return None


def mark_model_call_first_output(call_id: str | None, at: str | None = None) -> None:
    if not call_id:
        return
    try:
        with transaction() as conn:
            conn.execute(
                "UPDATE model_call_logs SET first_output_at=COALESCE(first_output_at, ?) WHERE id=?",
                (at or now_iso(), call_id),
            )
    except Exception:  # noqa: BLE001
        logger.exception("model_call_log_first_output_failed")


def finish_model_call(
    call_id: str | None,
    *,
    status: str,
    response_text: str = "",
    response: Any = None,
    error: BaseException | str | None = None,
    usage: dict[str, Any] | None = None,
    completed_at: str | None = None,
    parse_status: str | None = None,
    quality_status: str | None = None,
    adoption_status: str | None = None,
) -> None:
    if not call_id:
        return
    completed_at = completed_at or now_iso()
    usage = usage or {}
    response_text = str(response_text or "")
    if adoption_status is None and status in {"failed", "timeout", "canceled"}:
        adoption_status = "not_adopted"
    try:
        with transaction() as conn:
            row = conn.execute(
                "SELECT started_at, first_output_at FROM model_call_logs WHERE id=?",
                (call_id,),
            ).fetchone()
            first_output_at = row["first_output_at"] if row else None
            if response_text and not first_output_at:
                first_output_at = completed_at
            conn.execute(
                """
                UPDATE model_call_logs SET
                    status=?, response_text=?, response_json=?, error=?,
                    parse_status=COALESCE(?, parse_status),
                    quality_status=COALESCE(?, quality_status),
                    adoption_status=COALESCE(?, adoption_status),
                    first_output_at=?, completed_at=?, duration_ms=?, first_output_ms=?,
                    input_tokens=?, output_tokens=?, total_tokens=?
                WHERE id=?
                """,
                (
                    status,
                    response_text,
                    _dump(response) if response is not None else "",
                    _safe_error(error),
                    parse_status,
                    quality_status,
                    adoption_status,
                    first_output_at,
                    completed_at,
                    _elapsed_ms(row["started_at"], completed_at) if row else None,
                    _elapsed_ms(row["started_at"], first_output_at) if row and first_output_at else None,
                    usage.get("input_tokens"),
                    usage.get("output_tokens"),
                    usage.get("total_tokens"),
                    call_id,
                ),
            )
    except Exception:  # noqa: BLE001
        logger.exception("model_call_log_finish_failed")


def update_model_call_status(
    call_id: str | None,
    *,
    parse_status: str | None = None,
    quality_status: str | None = None,
    adoption_status: str | None = None,
    repair_of_call_id: str | None = None,
    only_if_transport_status: str | None = None,
) -> None:
    """Update lifecycle state without allowing task-level status to overwrite it."""
    if not call_id:
        return
    values: dict[str, Any] = {}
    for key, value in (
        ("parse_status", parse_status),
        ("quality_status", quality_status),
        ("adoption_status", adoption_status),
        ("repair_of_call_id", repair_of_call_id),
    ):
        if value is not None:
            values[key] = value
    if not values:
        return
    try:
        with transaction() as conn:
            where = "id=?"
            params: list[Any] = [*values.values(), call_id]
            if only_if_transport_status is not None:
                where += " AND status=?"
                params.append(only_if_transport_status)
            conn.execute(
                f"UPDATE model_call_logs SET {', '.join(f'{key}=?' for key in values)} WHERE {where}",
                params,
            )
    except Exception:  # noqa: BLE001
        logger.exception("model_call_log_status_update_failed")


def update_model_call_request(call_id: str | None, request: Any) -> None:
    """Replace the recorded request when the provider transport changes."""
    if not call_id:
        return
    try:
        with transaction() as conn:
            conn.execute(
                "UPDATE model_call_logs SET request_json=? WHERE id=?",
                (_dump(request), call_id),
            )
    except Exception:  # noqa: BLE001
        logger.exception("model_call_log_request_update_failed")


def finalize_generation_job_adoption(generation_job_id: str | None, adopted_call_id: str | None = None) -> None:
    """Mark only the final call output as adopted for a completed generation job."""
    if not generation_job_id:
        return
    try:
        with transaction() as conn:
            if adopted_call_id:
                conn.execute(
                    """
                    UPDATE model_call_logs
                    SET adoption_status='not_adopted'
                    WHERE generation_job_id=? AND id<>?
                      AND adoption_status IN ('pending', 'not_recorded')
                    """,
                    (generation_job_id, adopted_call_id),
                )
                conn.execute(
                    """
                    UPDATE model_call_logs
                    SET adoption_status='adopted'
                    WHERE generation_job_id=? AND id=?
                    """,
                    (generation_job_id, adopted_call_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE model_call_logs
                    SET adoption_status='not_adopted'
                    WHERE generation_job_id=?
                      AND adoption_status IN ('pending', 'not_recorded')
                    """,
                    (generation_job_id,),
                )
    except Exception:  # noqa: BLE001
        logger.exception("model_call_log_adoption_update_failed")


def _public(row: Any, *, include_payload: bool = False) -> dict[str, Any]:
    result = dict(row)
    request_raw = result.pop("request_json", "")
    response_raw = result.pop("response_json", "")
    job_input = _dict_json(json_loads(result.pop("generation_input_json", ""), {}))
    job_output = _dict_json(json_loads(result.pop("generation_output_json", ""), {}))
    request = _dict_json(json_loads(request_raw, {}))
    observations = _observability(request)
    work_id = result.get("work_id")
    generation_job_id = result.get("generation_job_id")
    generation_kind = result.get("generation_kind")
    planning_step = str(job_input.get("step") or request.get("step") or "")
    item_key = str(job_input.get("item_key") or request.get("item_key") or "")
    parse_status = (
        str(result.get("parse_status") or "not_recorded")
        if "parse_status" in result
        else str(job_output.get("parse_status") or ("not_applicable" if not generation_job_id else "not_recorded"))
    )
    quality_status = (
        str(result.get("quality_status") or "not_recorded")
        if "quality_status" in result
        else str(job_output.get("quality_status") or ("not_applicable" if not generation_job_id else "not_recorded"))
    )
    generation_status = result.get("generation_status")
    adoption_status = (
        str(result.get("adoption_status") or "not_recorded")
        if "adoption_status" in result
        else (
            "not_applicable"
            if not generation_job_id
            else str(job_output.get("adoption_status") or "not_recorded")
        )
    )
    context_counts = {
        str(key): int(value)
        for key, value in _dict_json(observations.get("context_char_counts")).items()
        if isinstance(value, (int, float))
    }
    context_total = int(observations.get("context_chars_total") or sum(context_counts.values()))
    context_shares = {
        key: round(value / context_total, 4) if context_total else 0
        for key, value in context_counts.items()
    }
    effective = {
        "provider": result.get("provider") or result.get("generation_resolved_provider") or "",
        "model": request.get("model") or result.get("model") or result.get("generation_resolved_model") or "",
        "base_url": result.get("base_url") or result.get("generation_resolved_base_url") or "",
    }
    for key in ("transport", "reasoning_effort", "temperature", "timeout_seconds", "stream", "extra_body"):
        if key in request:
            effective[key] = request[key]
    result["task_name"] = _task_name(generation_kind, job_input, result.get("call_kind"))
    result["planning_step"] = planning_step or None
    result["item_key"] = item_key or None
    result["generation_task_url"] = f"/works/{work_id}/generation-jobs/{generation_job_id}" if work_id and generation_job_id else None
    result["generation_status"] = generation_status
    result["transmission_status"] = {
        "running": "in_flight",
        "success": "delivered",
        "failed": "error",
        "timeout": "timeout",
        "canceled": "user_canceled",
    }.get(str(result.get("status")), str(result.get("status") or "unknown"))
    result["parse_status"] = parse_status
    result["quality_status"] = quality_status
    result["adoption_status"] = adoption_status
    result["failure_category"] = _failure_category(str(result.get("status") or ""), str(result.get("error") or ""), parse_status, quality_status)
    result["context_char_counts"] = context_counts
    result["context_chars_total"] = context_total
    result["context_char_shares"] = context_shares
    result["effective_parameters"] = effective
    result["repair_of_call_id"] = result.get("repair_of_call_id") or observations.get("repair_of_call_id")
    result.pop("generation_kind", None)
    result.pop("generation_resolved_provider", None)
    result.pop("generation_resolved_model", None)
    result.pop("generation_resolved_base_url", None)
    # Keep these projections independent from payload visibility.  The detail
    # endpoint and every call-chain item must expose the same stable counts as
    # the list endpoint, while still allowing the full payload to be omitted.
    result["request_chars"] = len(request_raw or "")
    result["response_chars"] = len(result.get("response_text") or "")
    if include_payload:
        result["request"] = request or request_raw
        result["response"] = json_loads(response_raw, response_raw) if response_raw else None
        result["response_text"] = result.get("response_text") or ""
    else:
        result.pop("response_text", None)
    return result


def list_model_calls(
    *,
    user_id: str = "demo-user",
    limit: int = 50,
    offset: int = 0,
    status: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    call_kind: str | None = None,
    work_id: str | None = None,
    from_at: str | None = None,
    to_at: str | None = None,
) -> dict[str, Any]:
    limit = min(max(int(limit), 1), 200)
    offset = max(int(offset), 0)
    clauses = ["c.user_id=?"]
    params: list[Any] = [user_id]
    for column, value in (
        ("c.status", status),
        ("c.provider", provider),
        ("c.model", model),
        ("c.call_kind", call_kind),
        ("c.work_id", work_id),
    ):
        if value:
            clauses.append(f"{column}=?")
            params.append(value)
    if from_at:
        clauses.append("c.created_at>=?")
        params.append(from_at)
    if to_at:
        clauses.append("c.created_at<=?")
        params.append(to_at)
    where = " AND ".join(clauses)
    with transaction() as conn:
        total = conn.execute(f"SELECT COUNT(*) AS count FROM model_call_logs AS c WHERE {where}", params).fetchone()["count"]
        rows = conn.execute(
            _joined_call_query(f"WHERE {where}", "ORDER BY c.created_at DESC LIMIT ? OFFSET ?"),
            (*params, limit, offset),
        ).fetchall()
    return {"items": [_public(row) for row in rows], "total": int(total), "limit": limit, "offset": offset}


def get_model_call(call_id: str, *, user_id: str = "demo-user") -> dict[str, Any] | None:
    with transaction() as conn:
        row = conn.execute(
            _joined_call_query("WHERE c.id=? AND c.user_id=?"),
            (call_id, user_id),
        ).fetchone()
        chain_rows = []
        if row and row["generation_job_id"]:
            chain_rows = conn.execute(
                _joined_call_query("WHERE c.generation_job_id=? AND c.user_id=?", "ORDER BY c.created_at ASC"),
                (row["generation_job_id"], user_id),
            ).fetchall()
    if not row:
        return None
    result = _public(row, include_payload=True)
    result["call_chain"] = [_public(item) for item in chain_rows]
    result["chain_root_call_id"] = result["call_chain"][0]["id"] if result["call_chain"] else result["id"]
    return result


def model_call_stats(*, user_id: str = "demo-user", work_id: str | None = None) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    today_start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc).isoformat()
    with transaction() as conn:
        clauses = ["c.user_id=?"]
        params: list[Any] = [user_id]
        if work_id:
            clauses.append("c.work_id=?")
            params.append(work_id)
        where = " AND ".join(clauses)
        rows = conn.execute(_joined_call_query(f"WHERE {where}"), params).fetchall()
        today = conn.execute(
            f"SELECT COUNT(*) AS count FROM model_call_logs AS c WHERE {where} AND c.created_at>=?",
            (*params, today_start),
        ).fetchone()["count"]
    public_rows = [_public(row) for row in rows]
    durations = sorted(int(item["duration_ms"]) for item in public_rows if item.get("duration_ms") is not None)
    total = len(public_rows)
    success = sum(1 for item in public_rows if item.get("status") == "success")
    failed = sum(1 for item in public_rows if item.get("status") == "failed")
    failure_categories = {
        category: sum(1 for item in public_rows if item.get("failure_category") == category)
        for category in ("timeout", "structure_error", "quality_failure", "user_canceled", "transport_failure")
    }
    p95 = durations[min(len(durations) - 1, max(0, int(len(durations) * 0.95) - 1))] if durations else None
    return {
        "total": total,
        "today": int(today or 0),
        "success": success,
        "failed": failed,
        "success_rate": round((success / total) * 100, 1) if total else 0,
        "avg_duration_ms": int(sum(durations) / len(durations)) if durations else None,
        "p95_duration_ms": p95,
        "total_tokens": sum(int(item.get("total_tokens") or 0) for item in public_rows),
        "failure_categories": failure_categories,
    }
