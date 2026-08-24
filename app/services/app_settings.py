"""Application-level runtime settings shared by API and worker processes."""

from __future__ import annotations

import socket
from typing import Any

from app.db import transaction
from app.utils import now_iso


DEFAULT_PROXY_HOST = "127.0.0.1"
DEFAULT_PROXY_PORT = 10808


def get_proxy_settings(user_id: str = "demo-user") -> dict[str, Any]:
    with transaction() as conn:
        row = conn.execute(
            "SELECT enabled, host, port, updated_at FROM proxy_settings WHERE user_id=?",
            (user_id,),
        ).fetchone()
    if not row:
        return {
            "enabled": False,
            "host": DEFAULT_PROXY_HOST,
            "port": DEFAULT_PROXY_PORT,
            "updated_at": None,
        }
    return {
        "enabled": bool(row["enabled"]),
        "host": str(row["host"] or DEFAULT_PROXY_HOST),
        "port": int(row["port"] or DEFAULT_PROXY_PORT),
        "updated_at": row["updated_at"],
    }


def save_proxy_settings(enabled: bool, port: int, user_id: str = "demo-user") -> dict[str, Any]:
    if not 1 <= int(port) <= 65535:
        raise ValueError("代理端口必须在 1 到 65535 之间")
    with transaction() as conn:
        conn.execute(
            """
            INSERT INTO proxy_settings(user_id, enabled, host, port, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                enabled=excluded.enabled, host=excluded.host,
                port=excluded.port, updated_at=excluded.updated_at
            """,
            (user_id, int(bool(enabled)), DEFAULT_PROXY_HOST, int(port), now_iso()),
        )
    return get_proxy_settings(user_id)


def proxy_url(settings: dict[str, Any]) -> str:
    return f"http://{settings['host']}:{int(settings['port'])}"


def test_proxy_port(port: int, host: str = DEFAULT_PROXY_HOST) -> dict[str, Any]:
    try:
        with socket.create_connection((host, int(port)), timeout=2):
            return {"ok": True, "message": f"代理端口 {host}:{int(port)} 正在监听"}
    except OSError as exc:
        return {"ok": False, "message": f"无法连接代理端口 {host}:{int(port)}：{exc}"}
