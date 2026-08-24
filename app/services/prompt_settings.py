"""Editable system prompts used by each generation stage."""

from __future__ import annotations

from typing import Any

from app.db import transaction
from app.utils import now_iso


PROMPT_METADATA: dict[str, dict[str, str]] = {
    "planning": {
        "title": "分阶段策划",
        "stage": "故事规划",
        "description": "用于创作契约、核心设定、人物阵容、人物小传等逐步确认任务。",
    },
    "setup": {
        "title": "故事档案生成",
        "stage": "故事规划",
        "description": "用于一次性生成故事档案、主要人物与卷级主线。",
    },
    "setup_repair": {
        "title": "故事档案修订",
        "stage": "质量修订",
        "description": "故事档案未通过质量检查时，用于自动修订完整方案。",
    },
    "outline": {
        "title": "章节大纲生成",
        "stage": "章节规划",
        "description": "用于把故事档案拆成连续、可执行的章节合同。",
    },
    "volume_outline": {
        "title": "卷纲草稿生成",
        "stage": "卷级规划",
        "description": "用于在固定分卷章节范围内生成可审核的卷纲与叙事阶段草稿。",
    },
    "outline_repair": {
        "title": "章节大纲修订",
        "stage": "质量修订",
        "description": "章节大纲未通过质量检查时，用于自动修订大纲。",
    },
    "chapter": {
        "title": "章节正文写作",
        "stage": "正文生成",
        "description": "用于初写、续写或重写单章正文。",
    },
    "editor": {
        "title": "章节责任编辑",
        "stage": "正文生成",
        "description": "正文生成后，在不改变事实的前提下进行编辑润色。",
    },
    "extraction": {
        "title": "章节状态提取",
        "stage": "状态维护",
        "description": "从正文中提取人物、时间线和伏笔等可审核事实。",
    },
    "trend": {
        "title": "热门灵感分析",
        "stage": "灵感分析",
        "description": "根据公开榜单元数据总结趋势并生成原创创意。",
    },
}


def get_prompt_setting(prompt_key: str, default_content: str, user_id: str = "demo-user") -> str:
    """Return the saved override, or the code default when no override exists."""
    with transaction() as conn:
        row = conn.execute(
            "SELECT content FROM prompt_settings WHERE user_id=? AND prompt_key=?",
            (user_id, prompt_key),
        ).fetchone()
    if not row:
        return default_content
    content = str(row["content"] or "").strip()
    return content or default_content


def list_prompt_settings(defaults: dict[str, str], user_id: str = "demo-user") -> list[dict[str, Any]]:
    with transaction() as conn:
        rows = conn.execute(
            "SELECT prompt_key, content, updated_at FROM prompt_settings WHERE user_id=?",
            (user_id,),
        ).fetchall()
    saved = {str(row["prompt_key"]): row for row in rows}
    items: list[dict[str, Any]] = []
    for key, metadata in PROMPT_METADATA.items():
        default_content = defaults[key]
        row = saved.get(key)
        items.append({
            "key": key,
            **metadata,
            "content": str(row["content"]) if row else default_content,
            "default_content": default_content,
            "is_customized": row is not None,
            "updated_at": row["updated_at"] if row else None,
        })
    return items


def save_prompt_setting(prompt_key: str, content: str, user_id: str = "demo-user") -> None:
    if prompt_key not in PROMPT_METADATA:
        raise KeyError(prompt_key)
    normalized = content.strip()
    if not normalized:
        raise ValueError("提示词不能为空")
    with transaction() as conn:
        conn.execute(
            """
            INSERT INTO prompt_settings(user_id, prompt_key, content, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id, prompt_key) DO UPDATE SET
                content=excluded.content, updated_at=excluded.updated_at
            """,
            (user_id, prompt_key, normalized, now_iso()),
        )


def reset_prompt_setting(prompt_key: str, user_id: str = "demo-user") -> bool:
    if prompt_key not in PROMPT_METADATA:
        raise KeyError(prompt_key)
    with transaction() as conn:
        cursor = conn.execute(
            "DELETE FROM prompt_settings WHERE user_id=? AND prompt_key=?",
            (user_id, prompt_key),
        )
    return cursor.rowcount > 0
