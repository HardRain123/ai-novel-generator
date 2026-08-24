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
    try:
        with transaction() as conn:
            conn.execute(
                """
                INSERT INTO model_call_logs(
                    id, user_id, work_id, generation_job_id, model_profile_id,
                    call_kind, provider, model, base_url, status,
                    request_json, started_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', ?, ?, ?)
                """,
                (
                    call_id,
                    user_id or context.user_id,
                    work_id if work_id is not None else context.work_id,
                    generation_job_id if generation_job_id is not None else context.generation_job_id,
                    profile.get("id"),
                    call_kind or context.call_kind,
                    str(profile.get("provider") or ""),
                    str(profile.get("model") or ""),
                    str(profile.get("base_url") or ""),
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
) -> None:
    if not call_id:
        return
    completed_at = completed_at or now_iso()
    usage = usage or {}
    response_text = str(response_text or "")
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
                    first_output_at=?, completed_at=?, duration_ms=?, first_output_ms=?,
                    input_tokens=?, output_tokens=?, total_tokens=?
                WHERE id=?
                """,
                (
                    status,
                    response_text,
                    _dump(response) if response is not None else "",
                    _safe_error(error),
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


def _public(row: Any, *, include_payload: bool = False) -> dict[str, Any]:
    result = dict(row)
    request_raw = result.pop("request_json", "")
    response_raw = result.pop("response_json", "")
    if include_payload:
        result["request"] = json_loads(request_raw, request_raw)
        result["response"] = json_loads(response_raw, response_raw) if response_raw else None
        result["response_text"] = result.get("response_text") or ""
    else:
        result["request_chars"] = len(request_raw or "")
        result["response_chars"] = len(result.get("response_text") or "")
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
    clauses = ["user_id=?"]
    params: list[Any] = [user_id]
    for column, value in (
        ("status", status),
        ("provider", provider),
        ("model", model),
        ("call_kind", call_kind),
        ("work_id", work_id),
    ):
        if value:
            clauses.append(f"{column}=?")
            params.append(value)
    if from_at:
        clauses.append("created_at>=?")
        params.append(from_at)
    if to_at:
        clauses.append("created_at<=?")
        params.append(to_at)
    where = " AND ".join(clauses)
    with transaction() as conn:
        total = conn.execute(f"SELECT COUNT(*) AS count FROM model_call_logs WHERE {where}", params).fetchone()["count"]
        rows = conn.execute(
            f"SELECT * FROM model_call_logs WHERE {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
    return {"items": [_public(row) for row in rows], "total": int(total), "limit": limit, "offset": offset}


def get_model_call(call_id: str, *, user_id: str = "demo-user") -> dict[str, Any] | None:
    with transaction() as conn:
        row = conn.execute(
            "SELECT * FROM model_call_logs WHERE id=? AND user_id=?",
            (call_id, user_id),
        ).fetchone()
    return _public(row, include_payload=True) if row else None


def model_call_stats(*, user_id: str = "demo-user") -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    today_start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc).isoformat()
    with transaction() as conn:
        summary = conn.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS success,
                   SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed,
                   AVG(duration_ms) AS avg_duration_ms,
                   SUM(COALESCE(total_tokens, 0)) AS total_tokens
            FROM model_call_logs WHERE user_id=?
            """,
            (user_id,),
        ).fetchone()
        today = conn.execute(
            "SELECT COUNT(*) AS count FROM model_call_logs WHERE user_id=? AND created_at>=?",
            (user_id, today_start),
        ).fetchone()["count"]
        durations = [
            int(row["duration_ms"])
            for row in conn.execute(
                "SELECT duration_ms FROM model_call_logs WHERE user_id=? AND duration_ms IS NOT NULL ORDER BY duration_ms",
                (user_id,),
            ).fetchall()
        ]
    p95 = durations[min(len(durations) - 1, max(0, int(len(durations) * 0.95) - 1))] if durations else None
    return {
        "total": int(summary["total"] or 0),
        "today": int(today or 0),
        "success": int(summary["success"] or 0),
        "failed": int(summary["failed"] or 0),
        "success_rate": round((int(summary["success"] or 0) / int(summary["total"])) * 100, 1) if summary["total"] else 0,
        "avg_duration_ms": int(summary["avg_duration_ms"]) if summary["avg_duration_ms"] is not None else None,
        "p95_duration_ms": p95,
        "total_tokens": int(summary["total_tokens"] or 0),
    }
