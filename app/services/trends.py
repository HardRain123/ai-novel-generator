"""Low-volume public ranking adapters with a small SQLite cache.

The service stores only public ranking metadata and short public descriptions. It
does not fetch or persist chapter/full-text content.
"""

from __future__ import annotations

import hashlib
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from html import unescape
from typing import Any
from uuid import uuid4

import httpx

from app.db import transaction
from app.services.model_profiles import resolve_profile
from app.services.novel_engine import engine
from app.utils import json_dumps, json_loads, now_iso

SOURCE_CONFIG = {
    "fanqie": {"label": "番茄小说", "url": "https://fanqienovel.com/rank/count"},
    "qidian": {"label": "起点中文网", "url": "https://www.qidian.com/rank/hotsales/"},
    "jjwxc": {"label": "晋江文学城", "url": "https://wap.jjwxc.net/rank/index"},
}
UA = "AI-Novel-Generator/0.3 (+local creative writing tool)"


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", value or ""))).strip()


def _extract_links(html: str, source: str) -> list[dict[str, Any]]:
    pattern = re.compile(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', re.I | re.S)
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for href, raw_text in pattern.findall(html):
        title = _clean(raw_text)
        if len(title) < 2 or len(title) > 80 or title in seen:
            continue
        if source == "fanqie" and not ("/page/" in href or "/reader/" in href or "novel" in href):
            continue
        if source == "qidian" and "/book/" not in href:
            continue
        if source == "jjwxc" and not ("onebook" in href or "book" in href):
            continue
        seen.add(title)
        url = href if href.startswith("http") else ("https://fanqienovel.com" + href if source == "fanqie" else "https://www.qidian.com" + href if source == "qidian" else "https://wap.jjwxc.net" + href)
        candidates.append({"title": title, "source_url": url})
        if len(candidates) >= 100:
            break
    return candidates


def _parse_source(source: str, html: str, category: str = "") -> list[dict[str, Any]]:
    links = _extract_links(html, source)
    if not links:
        raise ValueError(f"{SOURCE_CONFIG[source]['label']} 页面结构暂未识别")
    captured = now_iso()
    result = []
    for index, item in enumerate(links, start=1):
        source_id = hashlib.sha1(item["source_url"].encode("utf-8")).hexdigest()[:24]
        result.append({
            "source": source,
            "source_id": source_id,
            "rank": index,
            "board": "综合榜",
            "category": category,
            "title": item["title"],
            "author": "",
            "synopsis": "公开榜单条目；简介以来源页面为准。",
            "metric_label": "榜单排名",
            "metric_value": str(index),
            "source_url": item["source_url"],
            "captured_at": captured,
        })
    return result


def _fresh_snapshot(conn, source: str, category: str) -> Any:
    return conn.execute("SELECT * FROM trend_snapshots WHERE source=? AND category=? AND expires_at>? ORDER BY captured_at DESC LIMIT 1", (source, category, now_iso())).fetchone()


def _items_for_snapshot(conn, snapshot_id: str) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute("SELECT * FROM trend_items WHERE snapshot_id=? ORDER BY rank", (snapshot_id,)).fetchall()]


def refresh_source(source: str, category: str = "", force: bool = False) -> dict[str, Any]:
    if source not in SOURCE_CONFIG:
        raise ValueError(f"不支持的榜单来源：{source}")
    with transaction() as conn:
        if not force:
            cached = _fresh_snapshot(conn, source, category)
            if cached:
                return {"source": source, "stale": bool(cached["stale"]), "captured_at": cached["captured_at"], "items": _items_for_snapshot(conn, cached["id"])}
    captured = now_iso()
    try:
        with httpx.Client(timeout=20, follow_redirects=True, headers={"User-Agent": UA, "Accept-Language": "zh-CN"}) as client:
            response = client.get(SOURCE_CONFIG[source]["url"])
            response.raise_for_status()
        items = _parse_source(source, response.text, category)
        expires = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
        snapshot_id = str(uuid4())
        with transaction() as conn:
            conn.execute("INSERT INTO trend_snapshots(id,source,category,captured_at,expires_at,stale,error,created_at) VALUES (?,?,?,?,?,?,?,?)", (snapshot_id, source, category, captured, expires, 0, "", captured))
            for item in items:
                conn.execute("""INSERT INTO trend_items(id,snapshot_id,source,source_id,rank,board,category,title,author,synopsis,metric_label,metric_value,source_url,captured_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (str(uuid4()), snapshot_id, *[item[key] for key in ("source", "source_id", "rank", "board", "category", "title", "author", "synopsis", "metric_label", "metric_value", "source_url", "captured_at")]))
        return {"source": source, "stale": False, "captured_at": captured, "items": items}
    except Exception as exc:  # noqa: BLE001
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        with transaction() as conn:
            cached = conn.execute("SELECT * FROM trend_snapshots WHERE source=? AND category=? AND captured_at>? ORDER BY captured_at DESC LIMIT 1", (source, category, cutoff)).fetchone()
            if not cached:
                raise ValueError(f"{SOURCE_CONFIG[source]['label']} 暂时无法获取：{exc}") from exc
            conn.execute("UPDATE trend_snapshots SET stale=1,error=? WHERE id=?", (str(exc)[:500], cached["id"]))
            return {"source": source, "stale": True, "captured_at": cached["captured_at"], "error": str(exc), "items": _items_for_snapshot(conn, cached["id"])}


def search_trends(sources: list[str], category: str = "", board: str = "", keyword: str = "", refresh: bool = False) -> dict[str, Any]:
    all_items: list[dict[str, Any]] = []
    source_status: list[dict[str, Any]] = []
    unique_sources = list(dict.fromkeys(sources))
    results: dict[str, dict[str, Any] | Exception] = {}
    with ThreadPoolExecutor(max_workers=max(1, min(3, len(unique_sources)))) as pool:
        pending = {pool.submit(refresh_source, source, category, refresh): source for source in unique_sources}
        for future in as_completed(pending):
            source = pending[future]
            try:
                results[source] = future.result()
            except Exception as exc:  # noqa: BLE001 - one source must not block others
                results[source] = exc
    for source in unique_sources:
        result = results.get(source)
        if isinstance(result, Exception):
            source_status.append({"source": source, "stale": True, "error": str(result)})
        else:
            result = result or {}
            source_status.append({"source": source, "stale": result.get("stale", False), "captured_at": result.get("captured_at"), "error": result.get("error", "")})
            all_items.extend(result.get("items", []))
    needle = keyword.strip().lower()
    if needle:
        all_items = [item for item in all_items if needle in f"{item.get('title','')} {item.get('author','')} {item.get('synopsis','')}".lower()]
    if board:
        all_items = [item for item in all_items if item.get("board") == board]
    return {"items": all_items, "sources": source_status, "refreshed_at": now_iso()}


def analyze_trends(item_ids: list[str], model_profile_id: str | None = None) -> dict[str, Any]:
    with transaction() as conn:
        placeholders = ",".join("?" for _ in item_ids)
        rows = conn.execute(f"SELECT * FROM trend_items WHERE id IN ({placeholders})", item_ids).fetchall()
    items = [dict(row) for row in rows]
    if not items:
        raise ValueError("没有找到可分析的榜单条目")
    profile = resolve_profile(model_profile_id)
    if not profile or not profile.get("api_key"):
        raise ValueError("趋势分析需要先在模型服务中配置可用的 API Key")
    result = engine.generate_trend_ideas(items, profile)
    analysis_id = str(uuid4())
    with transaction() as conn:
        conn.execute("INSERT INTO trend_analyses(id,query_json,source_item_ids_json,result_json,model_profile_id,created_at) VALUES (?,?,?,?,?,?)", (analysis_id, json_dumps({"item_ids": item_ids}), json_dumps(item_ids), json_dumps(result), model_profile_id or (profile or {}).get("id"), now_iso()))
    return {"id": analysis_id, "items": items, **result}


def get_analysis(analysis_id: str) -> dict[str, Any] | None:
    with transaction() as conn:
        row = conn.execute("SELECT * FROM trend_analyses WHERE id=?", (analysis_id,)).fetchone()
        if not row:
            return None
        result = dict(row)
        result["query"] = json_loads(result.pop("query_json"), {})
        result["source_item_ids"] = json_loads(result.pop("source_item_ids_json"), [])
        data = json_loads(result.pop("result_json"), {})
        return {**result, **data}
