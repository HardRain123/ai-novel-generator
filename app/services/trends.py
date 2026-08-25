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
from urllib.parse import urljoin
from uuid import uuid4

import httpx

from app.db import transaction
from app.services.model_profiles import resolve_profile
from app.services.novel_engine import engine
from app.utils import json_dumps, json_loads, now_iso

SOURCE_CONFIG = {
    "fanqie": {"label": "番茄小说", "url": "https://fanqienovel.com/rank/count", "base_url": "https://fanqienovel.com"},
    # The desktop ranking page is protected by a JavaScript WAF challenge.  The
    # official mobile ranking page exposes the same public entries as regular
    # HTML and does not require an account session.
    "qidian": {"label": "起点中文网", "url": "https://m.qidian.com/rank/hotsales", "base_url": "https://m.qidian.com"},
    "jjwxc": {"label": "晋江文学城", "url": "https://wap.jjwxc.net/rank/index", "base_url": "https://wap.jjwxc.net"},
}
UA = "AI-Novel-Generator/0.3 (+local creative writing tool)"
FANQIE_CATEGORY_CONFIG_URL = "https://fanqienovel.com/api/config/list"
FANQIE_RANK_API_URL = "https://fanqienovel.com/api/rank/category/list"
FANQIE_DETAIL_URL = "https://fanqienovel.com/page/{book_id}"
FANQIE_RANK_HEADERS = {"Accept": "application/json, text/plain, */*", "Referer": SOURCE_CONFIG["fanqie"]["url"]}


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", value or ""))).strip()


def _has_decoding_damage(value: str) -> bool:
    """Detect irreversible replacement characters from a bad HTTP charset hint."""
    return "\ufffd" in value


def _has_font_obfuscation(value: str) -> bool:
    """The ranking API returns private-use glyphs for Chinese copy.

    Those glyphs only render correctly with a site-supplied font and must never
    be cached as a title.  The public book detail page contains ordinary HTML
    metadata, which is used by the Fanqie adapter below instead.
    """
    return any(0xE000 <= ord(char) <= 0xF8FF for char in value or "")


def _response_text(source: str, response: Any) -> str:
    """Decode source pages without trusting known-bad charset declarations.

    JJWXC intermittently labels legacy GBK/GB18030 pages as UTF-8.  Accessing
    ``response.text`` first loses bytes as U+FFFD, so only use the raw body to
    recover when that damage is detected.  Other adapters retain httpx's normal
    charset handling.
    """
    text = str(getattr(response, "text", ""))
    raw = getattr(response, "content", None)
    if source != "jjwxc" or not _has_decoding_damage(text) or not isinstance(raw, bytes):
        return text
    for encoding in ("gb18030", "gbk"):
        try:
            recovered = raw.decode(encoding)
        except UnicodeDecodeError:
            continue
        if not _has_decoding_damage(recovered):
            return recovered
    return text


def _is_usable_item(item: dict[str, Any]) -> bool:
    """Keep malformed landing-page links and bad-charset rows out of cache."""
    title = str(item.get("title") or "")
    url = str(item.get("source_url") or "")
    source = str(item.get("source") or "")
    if not title or _has_decoding_damage(title) or _has_font_obfuscation(title):
        return False
    if source == "fanqie":
        return "fanqienovel.com" in url and any(segment in url for segment in ("/page/", "/reader/", "/novel/"))
    return True


def _extract_links(html: str, source: str) -> list[dict[str, Any]]:
    pattern = re.compile(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', re.I | re.S)
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for href, raw_text in pattern.findall(html):
        title = _clean(raw_text)
        if len(title) < 2 or len(title) > 80 or title in seen:
            continue
        if source == "fanqie" and not ("/page/" in href or "/reader/" in href or "/novel/" in href):
            continue
        if source == "qidian" and not ("/book/" in href or "/info/" in href or "book.qidian.com/info/" in href):
            continue
        if source == "jjwxc" and not ("onebook" in href or "book" in href):
            continue
        seen.add(title)
        url = urljoin(SOURCE_CONFIG[source]["base_url"], href)
        candidates.append({"title": title, "source_url": url})
        if len(candidates) >= 100:
            break
    return candidates


def _attribute_value(attributes: str, name: str) -> str:
    match = re.search(rf"\b{re.escape(name)}\s*=\s*([\"'])(.*?)\1", attributes, re.I | re.S)
    return unescape(match.group(2)).strip() if match else ""


def _meta_description(html: str) -> str:
    for attributes in re.findall(r"<meta\b([^>]*)>", html, re.I | re.S):
        if _attribute_value(attributes, "name").lower() == "description":
            return _attribute_value(attributes, "content")
    return ""


def _fanqie_detail_metadata(html: str) -> tuple[str, str]:
    """Read plain-text title and intro from a public Fanqie book page."""
    title_match = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    page_title = _clean(title_match.group(1)) if title_match else ""
    title = re.split(r"(?:完整版)?在线免费阅读", page_title, maxsplit=1)[0].strip(" _-")
    description = _clean(_meta_description(html))
    if title and description:
        prefix = f"番茄小说提供{title}完整版在线免费阅读，精彩小说尽在番茄小说网。"
        if description.startswith(prefix):
            description = description[len(prefix):].strip()
    return title, description[:600]


def _fanqie_default_category(payload: dict[str, Any], requested: str) -> tuple[str, str, str]:
    categories = payload.get("data", {}).get("list", []) if isinstance(payload.get("data"), dict) else []
    if not isinstance(categories, list):
        categories = []
    requested = requested.strip()
    for item in categories:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("id") or "")
        name = str(item.get("name") or "")
        groups = item.get("group") if isinstance(item.get("group"), list) else []
        if requested and requested not in {item_id, name}:
            continue
        gender = "2" if "female" in groups else "1"
        return item_id, name or requested, gender
    if requested:
        raise ValueError(f"番茄小说未找到分类：{requested}")
    raise ValueError("番茄小说未返回可用榜单分类")


def _fetch_fanqie_items(client: Any, requested_category: str = "") -> list[dict[str, Any]]:
    """Fetch a small public Fanqie ranking without relying on rendered links."""
    category_response = client.get(
        FANQIE_CATEGORY_CONFIG_URL,
        params={"config_key": "serial_rank_category_list_common"},
        headers=FANQIE_RANK_HEADERS,
    )
    category_response.raise_for_status()
    category_id, category_name, gender = _fanqie_default_category(category_response.json(), requested_category)
    rank_response = client.get(
        FANQIE_RANK_API_URL,
        params={
            "app_id": 2503,
            "rank_list_type": 3,
            "offset": 0,
            "limit": 10,
            "category_id": category_id,
            "rank_version": "",
            "gender": gender,
            "rankMold": 2,
        },
        headers=FANQIE_RANK_HEADERS,
    )
    rank_response.raise_for_status()
    rank_payload = rank_response.json()
    if rank_payload.get("code") != 0:
        raise ValueError(f"番茄小说榜单接口返回异常：{rank_payload.get('message') or rank_payload.get('msg') or rank_payload.get('code')}")
    rank_data = rank_payload.get("data") if isinstance(rank_payload.get("data"), dict) else {}
    raw_books = rank_data.get("book_list") if isinstance(rank_data.get("book_list"), list) else []
    if not raw_books:
        raise ValueError("番茄小说榜单接口未返回作品条目")

    captured = now_iso()
    items: list[dict[str, Any]] = []
    detail_errors = 0
    for index, book in enumerate(raw_books[:10], start=1):
        if not isinstance(book, dict):
            continue
        book_id = str(book.get("bookId") or "")
        if not book_id:
            continue
        try:
            detail_response = client.get(FANQIE_DETAIL_URL.format(book_id=book_id))
            detail_response.raise_for_status()
            title, synopsis = _fanqie_detail_metadata(_response_text("fanqie", detail_response))
        except Exception:  # noqa: BLE001 - one unavailable detail must not discard the full board
            detail_errors += 1
            continue
        if not title or _has_font_obfuscation(title):
            detail_errors += 1
            continue
        rank = int(book.get("currentPos") or index)
        read_count = str(book.get("read_count") or book.get("readCount") or "")
        items.append({
            "source": "fanqie",
            "source_id": book_id,
            "rank": rank,
            "board": "男频阅读榜" if gender == "1" else "女频阅读榜",
            "category": category_name,
            "title": title,
            "author": "",
            "synopsis": synopsis or "公开榜单条目；简介以来源页面为准。",
            "metric_label": "在读",
            "metric_value": read_count or str(rank),
            "source_url": FANQIE_DETAIL_URL.format(book_id=book_id),
            "captured_at": captured,
        })
    if not items:
        suffix = "（详情页均未能读取）" if detail_errors else ""
        raise ValueError(f"番茄小说未返回可识别的公开作品条目{suffix}")
    return items


def _parse_qidian_mobile(html: str, category: str = "") -> list[dict[str, Any]]:
    """Extract records from Qidian's public mobile ranking card markup."""
    pattern = re.compile(r"<a\b(?P<attrs>[^>]*\bhref=[\"'][^\"']*(?:m\.qidian\.com/book/|/book/)\d+[^\"']*[\"'][^>]*)>(?P<body>.*?)</a>", re.I | re.S)
    captured = now_iso()
    items: list[dict[str, Any]] = []
    for index, match in enumerate(pattern.finditer(html), start=1):
        attrs, body = match.group("attrs"), match.group("body")
        href = _attribute_value(attrs, "href")
        title = re.sub(r"最新章节在线阅读$", "", _attribute_value(attrs, "title")).strip()
        book_id_match = re.search(r"/book/(\d+)", href)
        if not href or not title or not book_id_match:
            continue
        synopsis_match = re.search(r"<p[^>]*class=[\"'][^\"']*bookDesc[^\"']*[\"'][^>]*>(.*?)</p>", body, re.I | re.S)
        sub_match = re.search(r"<p[^>]*class=[\"'][^\"']*subTitle[^\"']*[\"'][^>]*>(.*?)</p>", body, re.I | re.S)
        rank_match = re.search(r"<div[^>]*class=[\"'][^\"']*ranking[^\"']*[\"'][^>]*>(\d+)</div>", body, re.I | re.S)
        sub_parts = [part.strip() for part in _clean(sub_match.group(1) if sub_match else "").split("·") if part.strip()]
        items.append({
            "source": "qidian",
            "source_id": book_id_match.group(1),
            "rank": int(rank_match.group(1)) if rank_match else index,
            "board": "畅销榜",
            "category": category or (sub_parts[1] if len(sub_parts) > 1 else ""),
            "title": title,
            "author": sub_parts[0] if sub_parts else "",
            "synopsis": _clean(synopsis_match.group(1) if synopsis_match else "") or "公开榜单条目；简介以来源页面为准。",
            "metric_label": "畅销排名",
            "metric_value": str(int(rank_match.group(1)) if rank_match else index),
            "source_url": urljoin(SOURCE_CONFIG["qidian"]["base_url"], href),
            "captured_at": captured,
        })
    return items


def _is_qidian_waf_response(response: Any) -> bool:
    headers = getattr(response, "headers", {}) or {}
    status = int(getattr(response, "status_code", 0) or 0)
    text = str(getattr(response, "text", ""))
    return status == 202 and ("X-WAF-UUID" in headers or "probe.js" in text)


def _parse_source(source: str, html: str, category: str = "") -> list[dict[str, Any]]:
    if source == "qidian":
        mobile_items = _parse_qidian_mobile(html, category)
        if mobile_items:
            return mobile_items
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


def _usable_snapshot_items(conn, snapshot_id: str) -> list[dict[str, Any]]:
    return [item for item in _items_for_snapshot(conn, snapshot_id) if _is_usable_item(item)]


def refresh_source(source: str, category: str = "", force: bool = False) -> dict[str, Any]:
    if source not in SOURCE_CONFIG:
        raise ValueError(f"不支持的榜单来源：{source}")
    with transaction() as conn:
        if not force:
            cached = _fresh_snapshot(conn, source, category)
            if cached:
                cached_items = _usable_snapshot_items(conn, cached["id"])
                if cached_items:
                    return {"source": source, "stale": bool(cached["stale"]), "captured_at": cached["captured_at"], "items": cached_items}
    captured = now_iso()
    try:
        with httpx.Client(timeout=20, follow_redirects=True, headers={"User-Agent": UA, "Accept-Language": "zh-CN"}) as client:
            if source == "fanqie":
                items = _fetch_fanqie_items(client, category)
            else:
                response = client.get(SOURCE_CONFIG[source]["url"])
                if source == "qidian" and _is_qidian_waf_response(response):
                    raise ValueError("起点中文网要求浏览器完成安全验证，移动榜单暂时不可用")
                response.raise_for_status()
                items = _parse_source(source, _response_text(source, response), category)
        items = [item for item in items if _is_usable_item(item)]
        if not items:
            raise ValueError(f"{SOURCE_CONFIG[source]['label']} 未返回可识别的作品条目")
        expires = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
        snapshot_id = str(uuid4())
        stored_items: list[dict[str, Any]] = []
        with transaction() as conn:
            conn.execute("INSERT INTO trend_snapshots(id,source,category,captured_at,expires_at,stale,error,created_at) VALUES (?,?,?,?,?,?,?,?)", (snapshot_id, source, category, captured, expires, 0, "", captured))
            for item in items:
                stored = {**item, "id": str(uuid4()), "snapshot_id": snapshot_id}
                conn.execute("""INSERT INTO trend_items(id,snapshot_id,source,source_id,rank,board,category,title,author,synopsis,metric_label,metric_value,source_url,captured_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (stored["id"], snapshot_id, *[stored[key] for key in ("source", "source_id", "rank", "board", "category", "title", "author", "synopsis", "metric_label", "metric_value", "source_url", "captured_at")]))
                stored_items.append(stored)
        return {"source": source, "stale": False, "captured_at": captured, "items": stored_items}
    except Exception as exc:  # noqa: BLE001
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        with transaction() as conn:
            snapshots = conn.execute("SELECT * FROM trend_snapshots WHERE source=? AND category=? AND captured_at>? ORDER BY captured_at DESC", (source, category, cutoff)).fetchall()
            for cached in snapshots:
                cached_items = _usable_snapshot_items(conn, cached["id"])
                if not cached_items:
                    continue
                conn.execute("UPDATE trend_snapshots SET stale=1,error=? WHERE id=?", (str(exc)[:500], cached["id"]))
                return {"source": source, "stale": True, "captured_at": cached["captured_at"], "error": str(exc), "items": cached_items}
            raise ValueError(f"{SOURCE_CONFIG[source]['label']} 暂时无法获取：{exc}") from exc


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
            # No snapshot was returned, so this is a live-source failure rather
            # than an offline-cache result.  The UI uses this distinction to
            # avoid claiming that unavailable data is a usable cached ranking.
            source_status.append({"source": source, "stale": False, "error": str(result)})
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


def _items_by_ids(conn, item_ids: list[str]) -> list[dict[str, Any]]:
    if not item_ids:
        return []
    placeholders = ",".join("?" for _ in item_ids)
    rows = conn.execute(f"SELECT * FROM trend_items WHERE id IN ({placeholders})", item_ids).fetchall()
    by_id = {str(row["id"]): dict(row) for row in rows}
    return [by_id[item_id] for item_id in item_ids if item_id in by_id]


def _fallback_source_model(item: dict[str, Any]) -> dict[str, Any]:
    category = str(item.get("category") or "未标注题材")
    return {
        "market_positioning": f"{category}读者对明确目标、快速冲突和持续悬念的需求。",
        "narrative_engine": {
            "opening": "从一个具体的失衡事件快速把主角推入选择。",
            "protagonist": "主角拥有可执行目标，同时受资源、身份或时间限制。",
            "conflict": "每次局部胜利都会换来更高成本或更强阻力。",
            "stakes": "失败会失去具体资源、关系或行动自由。",
        },
        "serial_engine": {"payoff_cadence": "短周期推进，卷末改变局面。", "hook_types": ["信息反转", "代价出现", "新任务"]},
        "safe_signals": ["明确目标", "具体代价", "持续升级"],
        "avoid_copying": ["不复用来源书名、人物、地点、物品或标志性设定", "不沿用来源关键事件顺序"],
    }


def _fallback_blueprint(idea: dict[str, Any], models: list[dict[str, Any]]) -> dict[str, Any]:
    signals = []
    for item in models:
        signals.extend(item.get("model", {}).get("safe_signals", []))
    return {
        "market_signals": list(dict.fromkeys(signals or ["明确目标", "具体代价", "持续升级"]))[:5],
        "creative_direction": idea.get("differentiation") or "用新的世界规则和人物选择兑现同类读者体验。",
        "transformation_contract": {
            "retain": ["高目标驱动", "阶段性回报", "结尾出现具体新局面"],
            "change": ["世界时代与社会环境", "主角身份和起点", "核心能力及代价", "人物关系拓扑", "冲突升级方式", "结局选择"],
            "entity_rules": {"characters": "全部重新命名并重写经历", "places": "全部重新命名并重建地理/组织关系", "items": "全部重新命名并改变用途、获取方式与代价"},
            "avoid": ["不得仅替换人名、地名、物品名", "不得沿用来源的标志性设定、专有词和关键场景链"],
        },
        "story_seed": {"hook": idea.get("hook", ""), "premise": idea.get("premise", ""), "reader_promise": "主角以主动选择换取阶段性胜利，并承担清晰代价。"},
    }


def _normalize_analysis_result(items: list[dict[str, Any]], result: dict[str, Any] | None) -> dict[str, Any]:
    result = dict(result) if isinstance(result, dict) else {}
    raw_models = result.get("source_models") if isinstance(result.get("source_models"), list) else []
    models_by_item = {
        str(model.get("trend_item_id")): model
        for model in raw_models if isinstance(model, dict) and str(model.get("trend_item_id") or "")
    }
    source_models = []
    for item in items:
        raw = models_by_item.get(str(item["id"]), {})
        model = raw.get("model") if isinstance(raw.get("model"), dict) else raw
        source_models.append({
            "trend_item_id": item["id"],
            "completeness": str(raw.get("completeness") or ("medium" if item.get("synopsis") else "low")),
            "model": model if isinstance(model, dict) and model else _fallback_source_model(item),
        })
    ideas = result.get("ideas") if isinstance(result.get("ideas"), list) else []
    normalized_ideas = []
    for raw in ideas[:5]:
        if not isinstance(raw, dict):
            continue
        idea = {key: str(raw.get(key) or "") for key in ("title", "genre", "audience", "hook", "premise", "synopsis", "differentiation", "risk")}
        if not idea["title"]:
            continue
        blueprint = raw.get("blueprint") if isinstance(raw.get("blueprint"), dict) else _fallback_blueprint(idea, source_models)
        idea["blueprint"] = blueprint
        normalized_ideas.append(idea)
    if not normalized_ideas:
        # The engine fallback normally supplies ideas.  This guard still leaves a
        # usable, transparent draft if a compatible model returns malformed JSON.
        for index, item in enumerate(items[:5], start=1):
            idea = {
                "title": f"未命名的{index}号远航", "genre": item.get("category") or "都市成长", "audience": "长篇连载读者",
                "hook": "一次具体的失衡事件迫使主角作出不可逆选择。", "premise": "主角必须在有限时间内以新的规则解决危机。",
                "synopsis": "以独立世界、人物关系和事件链兑现明确目标与升级回报。", "differentiation": "只吸收抽象节奏，不复用来源角色、设定或情节。", "risk": "需继续检查专有词、关系图和关键事件链。",
            }
            idea["blueprint"] = _fallback_blueprint(idea, source_models)
            normalized_ideas.append(idea)
    return {
        "trend_summary": str(result.get("trend_summary") or "已从公开榜单元数据提炼出可复用的市场信号。"),
        "rising_themes": [str(value) for value in result.get("rising_themes", []) if str(value).strip()][:8],
        "overcrowded_directions": [str(value) for value in result.get("overcrowded_directions", []) if str(value).strip()][:8],
        "source_models": source_models,
        "ideas": normalized_ideas,
    }


def _persist_analysis_artifacts(conn, analysis_id: str, items: list[dict[str, Any]], result: dict[str, Any]) -> None:
    model_ids: list[str] = []
    for source_model in result["source_models"]:
        model_id = str(uuid4())
        model_ids.append(model_id)
        conn.execute(
            "INSERT INTO source_work_models(id,analysis_id,trend_item_id,model_json,completeness,created_at) VALUES (?,?,?,?,?,?)",
            (model_id, analysis_id, source_model["trend_item_id"], json_dumps(source_model["model"]), source_model["completeness"], now_iso()),
        )
        source_model["id"] = model_id
    for index, idea in enumerate(result["ideas"]):
        blueprint_id = str(uuid4())
        content = {
            "title": idea["title"], "genre": idea["genre"], "audience": idea["audience"], "hook": idea["hook"],
            "premise": idea["premise"], "synopsis": idea["synopsis"], "differentiation": idea["differentiation"],
            "creative_brief": idea["blueprint"], "source_model_ids": model_ids,
        }
        originality = {
            "status": "pending_author_review",
            "checks": ["全部人物、地点、物品和组织须重新命名并重写功能", "不得复用来源标志性设定、专有词或关键事件链", "故事档案生成后需复核关系图、升级机制和结局逻辑"],
            "risk": idea["risk"],
        }
        conn.execute(
            "INSERT INTO inspiration_blueprints(id,analysis_id,idea_index,content_json,originality_json,created_at) VALUES (?,?,?,?,?,?)",
            (blueprint_id, analysis_id, index, json_dumps(content), json_dumps(originality), now_iso()),
        )
        idea["blueprint_id"] = blueprint_id


def analyze_trends(item_ids: list[str], model_profile_id: str | None = None) -> dict[str, Any]:
    with transaction() as conn:
        items = _items_by_ids(conn, item_ids)
    if not items:
        raise ValueError("没有找到可分析的榜单条目")
    profile = resolve_profile(model_profile_id)
    if not profile or (not profile.get("api_key") and profile.get("provider") != "codex_auth"):
        raise ValueError("趋势分析需要先在模型服务中配置可用的 API Key")
    result = _normalize_analysis_result(items, engine.generate_trend_ideas(items, profile))
    analysis_id = str(uuid4())
    with transaction() as conn:
        conn.execute("INSERT INTO trend_analyses(id,query_json,source_item_ids_json,result_json,model_profile_id,created_at) VALUES (?,?,?,?,?,?)", (analysis_id, json_dumps({"item_ids": item_ids}), json_dumps(item_ids), json_dumps(result), model_profile_id or (profile or {}).get("id"), now_iso()))
        _persist_analysis_artifacts(conn, analysis_id, items, result)
        conn.execute("UPDATE trend_analyses SET result_json=? WHERE id=?", (json_dumps(result), analysis_id))
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
        items = _items_by_ids(conn, result["source_item_ids"])
        models = conn.execute("SELECT * FROM source_work_models WHERE analysis_id=? ORDER BY created_at", (analysis_id,)).fetchall()
        blueprints = conn.execute("SELECT * FROM inspiration_blueprints WHERE analysis_id=? ORDER BY idea_index", (analysis_id,)).fetchall()
        data["source_models"] = [{
            "id": row["id"], "trend_item_id": row["trend_item_id"], "completeness": row["completeness"], "model": json_loads(row["model_json"], {}),
        } for row in models]
        blueprint_by_index = {int(row["idea_index"]): row["id"] for row in blueprints}
        for index, idea in enumerate(data.get("ideas") if isinstance(data.get("ideas"), list) else []):
            if isinstance(idea, dict):
                idea["blueprint_id"] = blueprint_by_index.get(index, idea.get("blueprint_id"))
        return {**result, "items": items, **data}


def get_blueprint(blueprint_id: str) -> dict[str, Any] | None:
    with transaction() as conn:
        row = conn.execute("SELECT * FROM inspiration_blueprints WHERE id=?", (blueprint_id,)).fetchone()
        if not row:
            return None
        return {**dict(row), "content": json_loads(row["content_json"], {}), "originality": json_loads(row["originality_json"], {})}
