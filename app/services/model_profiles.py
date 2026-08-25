"""Model profile storage, redaction and OpenAI-compatible connectivity."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import shutil
import subprocess
import json
from threading import Lock
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

import httpx
try:
    from cryptography.fernet import Fernet, InvalidToken
except ModuleNotFoundError:  # pragma: no cover - production installs cryptography
    Fernet = None  # type: ignore[assignment,misc]
    InvalidToken = ValueError  # type: ignore[assignment,misc]

from app.config import (
    ALLOW_INSECURE_MODEL_URLS,
    APP_SECRET_KEY,
    LLM_API_KEY,
    LLM_BASE_URL,
    LLM_MODEL,
    LLM_TIMEOUT_SECONDS,
)
from app.db import transaction
from app.services.model_call_logs import finish_model_call, model_call_context, start_model_call
from app.utils import json_dumps, now_iso

PRESETS: dict[str, dict[str, Any]] = {
    "deepseek": {"name": "DeepSeek", "base_url": "https://api.deepseek.com", "model": "deepseek-v4-flash"},
    "qwen": {"name": "通义千问", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen-plus"},
    "kimi": {"name": "Kimi", "base_url": "https://api.moonshot.cn/v1", "model": "kimi-k2.5"},
    "custom": {"name": "自定义 OpenAI 兼容", "base_url": "https://", "model": ""},
    "codex_auth": {"name": "Codex Auth（本机 Codex 登录）", "base_url": "codex://local", "model": "gpt-5.6-sol", "timeout_seconds": 600},
}

# A connection test sends a fixed, tiny JSON prompt. It should prove that the
# transport works quickly and must not reserve a worker thread for the full
# generation timeout.
CODEX_CONNECTION_TEST_TIMEOUT_SECONDS = 120.0
_connection_test_lock = Lock()


def _secret_bytes() -> bytes:
    return hashlib.sha256((APP_SECRET_KEY or "development-only-model-secret").encode("utf-8")).digest()


def _fernet() -> Fernet | None:
    if Fernet is None:
        return None
    raw = APP_SECRET_KEY.encode("utf-8") if APP_SECRET_KEY else b"development-only-model-secret"
    try:
        key = raw if len(raw) == 44 else base64.urlsafe_b64encode(hashlib.sha256(raw).digest())
        return Fernet(key)
    except (ValueError, TypeError):
        return Fernet(base64.urlsafe_b64encode(hashlib.sha256(raw).digest()))


def encrypt_secret(value: str) -> str:
    if not value:
        return ""
    fernet = _fernet()
    if fernet:
        return fernet.encrypt(value.encode("utf-8")).decode("ascii")
    nonce = os.urandom(16)
    stream = hashlib.sha256(_secret_bytes() + nonce).digest()
    cipher = bytes(byte ^ stream[index % len(stream)] for index, byte in enumerate(value.encode("utf-8")))
    tag = hmac.new(_secret_bytes(), nonce + cipher, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(nonce + tag + cipher).decode("ascii")


def decrypt_secret(value: str) -> str:
    if not value:
        return ""
    try:
        fernet = _fernet()
        if fernet:
            return fernet.decrypt(value.encode("ascii")).decode("utf-8")
        raw = base64.urlsafe_b64decode(value.encode("ascii"))
        nonce, tag, cipher = raw[:16], raw[16:48], raw[48:]
        if not hmac.compare_digest(tag, hmac.new(_secret_bytes(), nonce + cipher, hashlib.sha256).digest()):
            return ""
        stream = hashlib.sha256(_secret_bytes() + nonce).digest()
        plain = bytes(byte ^ stream[index % len(stream)] for index, byte in enumerate(cipher))
        return plain.decode("utf-8")
    except (InvalidToken, ValueError, UnicodeDecodeError):
        return ""


def mask_secret(value: str) -> str:
    if not value:
        return ""
    return f"{value[:3]}***{value[-4:]}" if len(value) > 8 else "***"


def normalize_base_url(value: str) -> str:
    url = value.strip().rstrip("/")
    parsed = urlparse(url)
    if parsed.scheme not in {"https", "http"} or not parsed.netloc:
        raise ValueError("Base URL 必须是完整的 http(s) 地址")
    host = (parsed.hostname or "").lower()
    if parsed.scheme == "http" and not ALLOW_INSECURE_MODEL_URLS and host not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("生产环境只允许 HTTPS Base URL")
    if url.endswith("/chat/completions") or url.endswith("/models"):
        raise ValueError("请填写 API 根地址，不要填写 /chat/completions 或 /models")
    return url


def _public(row: dict[str, Any]) -> dict[str, Any]:
    encrypted = row.pop("encrypted_api_key", "")
    row["has_api_key"] = bool(encrypted)
    row["api_key_masked"] = mask_secret(decrypt_secret(encrypted)) if encrypted else ""
    # Never expose provider response bodies or connection diagnostics from the
    # profile listing; an upstream error can accidentally echo request data.
    row.pop("last_test_error", None)
    return row


def _row(row) -> dict[str, Any] | None:
    return _public(dict(row)) if row else None


def _codex_command() -> str | None:
    configured = os.getenv("CODEX_CLI_PATH", "").strip()
    if configured and os.path.isfile(configured):
        return configured
    return shutil.which("codex") or shutil.which("codex.cmd")


def codex_auth_status() -> dict[str, Any]:
    command = _codex_command()
    if not command:
        return {"ok": False, "message": "未找到 Codex CLI，请先安装 Codex CLI"}
    try:
        from app.services.novel_engine import codex_process_env

        result = subprocess.run(
            [command, "login", "status"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
            env=codex_process_env(),
        )
    except OSError as exc:
        return {"ok": False, "message": f"无法启动 Codex CLI：{exc}"}
    if result.returncode != 0:
        return {"ok": False, "message": "Codex 尚未登录，请先在运行 worker 的机器执行 codex login"}
    return {"ok": True, "message": "Codex Auth 已登录"}


def list_profiles(user_id: str = "demo-user") -> list[dict[str, Any]]:
    with transaction() as conn:
        rows = conn.execute(
            "SELECT * FROM model_profiles WHERE user_id=? ORDER BY is_default DESC, updated_at DESC", (user_id,)
        ).fetchall()
        return [_row(row) for row in rows]


def get_profile(profile_id: str, user_id: str = "demo-user") -> dict[str, Any] | None:
    with transaction() as conn:
        return _row(conn.execute("SELECT * FROM model_profiles WHERE id=? AND user_id=?", (profile_id, user_id)).fetchone())


def _get_secret(profile_id: str, user_id: str = "demo-user") -> tuple[dict[str, Any] | None, str]:
    with transaction() as conn:
        row = conn.execute("SELECT * FROM model_profiles WHERE id=? AND user_id=?", (profile_id, user_id)).fetchone()
        return (dict(row), decrypt_secret(row["encrypted_api_key"]) if row else "")


def resolve_profile(
    profile_id: str | None = None,
    work_id: str | None = None,
    user_id: str = "demo-user",
    *,
    require_requested_profile: bool = False,
) -> dict[str, Any] | None:
    """Resolve a usable profile without silently changing an explicit choice.

    A job may intentionally omit a profile and use the work/default model.  Once a
    profile id has been supplied, however, falling back to another provider makes
    the UI lie about the model that produced the text.  Callers that create jobs
    therefore use ``require_requested_profile``.
    """
    with transaction() as conn:
        row = None
        if profile_id:
            row = conn.execute("SELECT * FROM model_profiles WHERE id=? AND user_id=? AND enabled=1", (profile_id, user_id)).fetchone()
            if not row and require_requested_profile:
                raise ValueError("指定的模型配置不存在或已停用，请重新选择模型后再生成")
        if not row and work_id:
            row = conn.execute(
                "SELECT p.* FROM works w JOIN model_profiles p ON p.id=w.model_profile_id WHERE w.id=? AND w.user_id=? AND p.enabled=1",
                (work_id, user_id),
            ).fetchone()
        if not row:
            row = conn.execute("SELECT * FROM model_profiles WHERE user_id=? AND enabled=1 AND (encrypted_api_key<>'' OR (provider='codex_auth' AND last_test_status='ok')) ORDER BY is_default DESC, updated_at DESC LIMIT 1", (user_id,)).fetchone()
        if not row:
            row = conn.execute("SELECT * FROM model_profiles WHERE user_id=? AND enabled=1 ORDER BY is_default DESC, updated_at DESC LIMIT 1", (user_id,)).fetchone()
        if not row:
            return None
        result = dict(row)
        result["api_key"] = decrypt_secret(result.pop("encrypted_api_key", ""))
        return result


TASK_REASONING_EFFORT = {
    "setup": "medium",
    "outline": "medium",
    "volume_outline": "medium",
    "planning_step": "medium",
    "planning_character_batch": "medium",
    "chapter": "high",
    "state_extraction": "low",
}


def profile_for_task(profile: dict[str, Any] | None, kind: str) -> dict[str, Any] | None:
    """Apply product-level reasoning policy without mutating the saved profile."""
    if not profile:
        return None
    result = dict(profile)
    effort = TASK_REASONING_EFFORT.get(kind)
    if effort:
        result["reasoning_effort"] = effort
    if result.get("provider") == "codex_auth":
        # High-reasoning chapter jobs regularly need more than the old preset's
        # 180 seconds. Keep a safety bound, but never apply the obsolete cutoff.
        result["timeout_seconds"] = max(float(result.get("timeout_seconds") or 0), 600)
    return result


def create_profile(payload: dict[str, Any], user_id: str = "demo-user") -> dict[str, Any]:
    provider = payload.get("provider", "openai_compatible")
    base_url = "codex://local" if provider == "codex_auth" else normalize_base_url(payload["base_url"])
    name = payload["name"].strip()
    now = now_iso()
    profile_id = str(uuid4())
    with transaction() as conn:
        duplicate = conn.execute(
            "SELECT id FROM model_profiles WHERE user_id=? AND name=? LIMIT 1",
            (user_id, name),
        ).fetchone()
        if duplicate:
            raise ValueError(f"模型配置名称已存在：{name}，请直接编辑已有配置")
        if payload.get("is_default"):
            conn.execute("UPDATE model_profiles SET is_default=0 WHERE user_id=?", (user_id,))
        conn.execute(
            """INSERT INTO model_profiles(id,user_id,name,provider,base_url,model,encrypted_api_key,reasoning_effort,
            timeout_seconds,is_default,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (profile_id, user_id, name, provider, base_url,
             payload["model"].strip(), encrypt_secret(payload.get("api_key", "")), payload.get("reasoning_effort", "auto"),
             payload.get("timeout_seconds", LLM_TIMEOUT_SECONDS), int(bool(payload.get("is_default"))), now, now),
        )
    return get_profile(profile_id, user_id)  # type: ignore[return-value]


def update_profile(profile_id: str, payload: dict[str, Any], user_id: str = "demo-user") -> dict[str, Any] | None:
    with transaction() as conn:
        row = conn.execute("SELECT * FROM model_profiles WHERE id=? AND user_id=?", (profile_id, user_id)).fetchone()
        if not row:
            return None
        values: dict[str, Any] = {}
        for key in ("name", "provider", "model", "reasoning_effort", "timeout_seconds", "enabled"):
            if payload.get(key) is not None:
                values[key] = payload[key]
        if payload.get("provider") == "codex_auth":
            values["base_url"] = "codex://local"
        elif payload.get("base_url") is not None:
            values["base_url"] = normalize_base_url(payload["base_url"])
        if payload.get("api_key") is not None:
            values["encrypted_api_key"] = encrypt_secret(payload["api_key"])
        elif payload.get("clear_api_key"):
            values["encrypted_api_key"] = ""
        if payload.get("is_default") is True:
            conn.execute("UPDATE model_profiles SET is_default=0 WHERE user_id=?", (user_id,))
            values["is_default"] = 1
        elif payload.get("is_default") is False:
            values["is_default"] = 0
        if values:
            values["updated_at"] = now_iso()
            assignments = ", ".join(f"{key}=?" for key in values)
            conn.execute(f"UPDATE model_profiles SET {assignments} WHERE id=? AND user_id=?", (*values.values(), profile_id, user_id))
    return get_profile(profile_id, user_id)


def delete_profile(profile_id: str, user_id: str = "demo-user") -> bool:
    with transaction() as conn:
        cur = conn.execute("DELETE FROM model_profiles WHERE id=? AND user_id=?", (profile_id, user_id))
        return cur.rowcount > 0


def _request_payload(profile: dict[str, Any], prompt: str = "返回 JSON：{\"ok\":true}") -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": profile["model"],
        "messages": [{"role": "system", "content": "你是连接测试助手，只返回合法 JSON。"}, {"role": "user", "content": prompt}],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    provider = str(profile.get("provider") or "openai_compatible")
    if provider in {"openai", "openai_compatible"} and profile.get("reasoning_effort") and profile["reasoning_effort"] != "auto":
        body["reasoning_effort"] = profile["reasoning_effort"]
    if provider == "deepseek" and profile.get("reasoning_effort") in {"high", "xhigh"}:
        body["thinking"] = {"type": "enabled"}
    return body


def test_profile(profile_id: str, user_id: str = "demo-user") -> dict[str, Any]:
    if not _connection_test_lock.acquire(blocking=False):
        raise ValueError("已有模型连接测试正在进行，请等待其完成后再试")
    try:
        return _test_profile(profile_id, user_id)
    finally:
        _connection_test_lock.release()


def _test_profile(profile_id: str, user_id: str = "demo-user") -> dict[str, Any]:
    raw, api_key = _get_secret(profile_id, user_id)
    if not raw:
        raise ValueError("模型配置不存在")
    if raw.get("provider") == "codex_auth":
        login = codex_auth_status()
        error = ""
        if login["ok"]:
            try:
                from app.services.novel_engine import engine

                with model_call_context(user_id=user_id, call_kind="connection_test"):
                    probe_profile = {
                        **raw,
                        "timeout_seconds": min(
                            float(raw.get("timeout_seconds") or LLM_TIMEOUT_SECONDS),
                            CODEX_CONNECTION_TEST_TIMEOUT_SECONDS,
                        ),
                    }
                    engine.probe_codex_auth(probe_profile)
            except Exception as exc:  # noqa: BLE001
                error = str(exc)
        else:
            error = login["message"]
        ok = not error
        with transaction() as conn:
            conn.execute("UPDATE model_profiles SET last_test_status=?, last_test_error=?, last_test_at=?, updated_at=? WHERE id=? AND user_id=?",
                         ("ok" if ok else "failed", error, now_iso(), now_iso(), profile_id, user_id))
        if not ok:
            raise ValueError(error)
        return {"ok": True, "message": "Codex Auth 登录与模型调用均正常", "auth_mode": "codex_auth"}
    if not api_key:
        raise ValueError("请先填写 API Key")
    base_url = normalize_base_url(raw["base_url"])
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    result: dict[str, Any] = {"ok": False, "models_supported": False}
    error = ""
    request_payload = _request_payload(raw)
    call_id = start_model_call(raw, request_payload, call_kind="connection_test", user_id=user_id)
    response_text = ""
    call_finished = False
    try:
        with httpx.Client(timeout=float(raw["timeout_seconds"]), headers=headers) as client:
            models_response = client.get(f"{base_url}/models")
            if models_response.is_success:
                result["models_supported"] = True
                result["models"] = [item.get("id") for item in models_response.json().get("data", []) if item.get("id")]
            response = client.post(f"{base_url}/chat/completions", json=request_payload)
            response_text = response.text
            if not response.is_success:
                error = f"HTTP {response.status_code}: {response.text[:500]}"
            else:
                result["ok"] = True
                result["model"] = response.json().get("model", raw["model"])
                finish_model_call(call_id, status="success", response_text=response_text, response=response.json())
                call_finished = True
    except Exception as exc:  # noqa: BLE001
        error = str(exc)
    if not call_finished:
        finish_model_call(call_id, status="failed", response_text=response_text, error=error)
    with transaction() as conn:
        conn.execute("UPDATE model_profiles SET last_test_status=?, last_test_error=?, last_test_at=?, updated_at=? WHERE id=? AND user_id=?",
                     ("ok" if result["ok"] else "failed", error, now_iso(), now_iso(), profile_id, user_id))
    if not result["ok"]:
        raise ValueError(error or "模型连接测试失败")
    result["message"] = "连接成功"
    return result


def fetch_models(profile_id: str, user_id: str = "demo-user") -> list[str]:
    raw, api_key = _get_secret(profile_id, user_id)
    if not raw:
        return []
    if raw.get("provider") == "codex_auth":
        command = _codex_command()
        if not command:
            return []
        from app.services.novel_engine import codex_process_env

        result = subprocess.run(
            [command, "debug", "models"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
            env=codex_process_env(),
        )
        if result.returncode != 0:
            return []
        models: list[str] = []
        for line in result.stdout.splitlines():
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            candidates = value if isinstance(value, list) else value.get("models", value.get("data", [])) if isinstance(value, dict) else []
            if isinstance(candidates, list):
                for item in candidates:
                    model_id = (item.get("id") or item.get("slug")) if isinstance(item, dict) else str(item)
                    if model_id and model_id not in models:
                        models.append(model_id)
        return models
    if not api_key:
        return []
    with httpx.Client(timeout=float(raw["timeout_seconds"]), headers={"Authorization": f"Bearer {api_key}"}) as client:
        response = client.get(f"{normalize_base_url(raw['base_url'])}/models")
        response.raise_for_status()
        return [item.get("id") for item in response.json().get("data", []) if item.get("id")]


def bootstrap_legacy_profile(user_id: str = "demo-user") -> None:
    # Migrate the old Codex preset value so the settings page matches the
    # effective task timeout. Values explicitly changed to anything else stay.
    with transaction() as conn:
        conn.execute(
            "UPDATE model_profiles SET timeout_seconds=600, updated_at=? WHERE user_id=? AND provider='codex_auth' AND timeout_seconds=180",
            (now_iso(), user_id),
        )
    if not LLM_API_KEY:
        return
    with transaction() as conn:
        exists = conn.execute("SELECT 1 FROM model_profiles WHERE user_id=? LIMIT 1", (user_id,)).fetchone()
    if exists:
        return
    create_profile({"name": "旧环境变量配置", "base_url": LLM_BASE_URL, "model": LLM_MODEL, "api_key": LLM_API_KEY,
                    "timeout_seconds": LLM_TIMEOUT_SECONDS, "is_default": True}, user_id)


def preset(name: str) -> dict[str, Any]:
    if name not in PRESETS:
        raise ValueError("未知模型预设")
    return dict(PRESETS[name])
