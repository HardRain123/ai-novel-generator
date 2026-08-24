import json
import os
import sqlite3
import tempfile
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient


DB_PATH = Path(tempfile.gettempdir()) / "ai-novel-generator-test.db"
if DB_PATH.exists():
    DB_PATH.unlink()
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"
os.environ.pop("LLM_API_KEY", None)

from app.main import app  # noqa: E402
from app.services.context_builder import build_context, chapter_generation_context  # noqa: E402
from app.services.character_cards import compact_character, planning_character  # noqa: E402
from app.services.generation_jobs import run_worker_once  # noqa: E402
from app.services import model_profiles, novel_engine  # noqa: E402
from app.services.model_profiles import profile_for_task  # noqa: E402
from app.services.novel_engine import NovelEngine, _parse_json, codex_process_env, configured_prompt, engine  # noqa: E402
from app.services.planning_quality import evaluate_outline, language_risks, planning_checks  # noqa: E402
from app.services import trends  # noqa: E402
from app.services.generation_jobs import _planning_context  # noqa: E402
from app.services.repository import get_work  # noqa: E402
from app.db import transaction  # noqa: E402
from app.utils import now_iso  # noqa: E402


@pytest.fixture()
def client():
    with TestClient(app) as test_client:
        yield test_client


def test_health(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_model_json_parser_keeps_first_complete_value_when_provider_appends_data():
    assert _parse_json('{"chapters":[{"chapter_no":2}]}\n{"note":"duplicate"}') == {
        "chapters": [{"chapter_no": 2}]
    }


def test_chapter_generation_context_is_compact_and_keeps_emotional_inputs():
    base = {
        "chapter_plan": {
            "chapter_no": 3, "title": "守门", "pov_character": "陈野",
            "goal": "守住入口", "conflict": "材料不足", "failure_cost": "同伴会被困在门外",
            "beats": ["发现危机", "做出选择"], "character_arc_progress": "陈野第一次为同伴承担损失",
            "appearing_characters": ["陈野", "苏晚晴"], "appearing_factions": ["守夜人"],
        },
        "next_chapter_boundary": {
            "chapter_no": 4, "title": "出门", "goal": "寻找材料", "conflict": "街道被封锁",
            "beats": ["不应发送的下一章节点"], "hook": "不应发送的钩子",
        },
        "long_term_rules": {"summary": "故事概要", "style_rules": "快节奏", "updated_at": "不应发送"},
        "characters": [
            {"name": "陈野", "role": "主角", "story_function": "不应发送", "personality": "克制", "voice": "短句", "relationships": "在意苏晚晴", "dramatic_core": {"motivation": "保护同伴", "flaw": "固执"}},
            {"name": "苏晚晴", "role": "护士", "personality": "果断", "dramatic_core": {"motivation": "救人"}},
            {"name": "赵海龙", "role": "反派", "personality": "残酷", "dramatic_core": {"motivation": "夺权"}},
        ],
        "previous_chapters": [{"chapter_no": 2, "title": "前章", "excerpt": "a" * 1200}],
        "active_factions": [{"name": "守夜人", "description": "盟友", "state": "active"}, {"name": "铁狼会", "description": "敌人"}],
        "confirmed_timeline": [], "open_foreshadows": [], "continuity_warnings": [], "excluded": [],
        "fact_version": 2, "plan_version": 4,
    }

    context = chapter_generation_context(base)

    assert "chapter_plan" not in context
    assert context["chapter_contract"]["goal"] == "守住入口"
    assert context["emotional_direction"]["pov_arc_step"] == "陈野第一次为同伴承担损失"
    assert [item["name"] for item in context["relevant_characters"]] == ["陈野", "苏晚晴"]
    assert "story_function" not in context["relevant_characters"][0]
    assert context["next_chapter_boundary"] == {"chapter_no": 4, "title": "出门", "goal": "寻找材料", "conflict": "街道被封锁"}
    assert len(context["previous_chapters"][0]["excerpt"]) == 900


def test_chapter_writer_sends_compact_contract_only_once(monkeypatch):
    captured = {}
    compact_context = {
        "chapter_contract": {"chapter_no": 1, "title": "守门", "goal": "守住入口"},
        "relevant_characters": [{"name": "陈野", "personality": "克制"}],
        "emotional_direction": {"pov_arc_step": "第一次承担风险"},
        "fact_version": 0,
        "outline_version": 1,
    }

    def fake_llm(_self, _system, user, *_args, **_kwargs):
        captured.update(json.loads(user))
        return {"chapter_no": 1, "title": "守门", "content": "正文", "continuity_warnings": []}

    monkeypatch.setattr("app.services.novel_engine.build_chapter_generation_context", lambda *_args: compact_context)
    monkeypatch.setattr("app.services.novel_engine.record_context_audit", lambda *_args: "audit-id")
    monkeypatch.setattr("app.services.novel_engine.configured_prompt", lambda *_args: "system")
    monkeypatch.setattr(NovelEngine, "_llm_json", fake_llm)

    result = NovelEngine()._write_chapter(
        {"id": "work-1", "chapter_plans": [{"chapter_no": 1, "title": "守门"}]},
        1,
        "chapter",
    )

    assert result["content"] == "正文"
    assert captured["chapter_contract"] == compact_context["chapter_contract"]
    assert "chapter_contract" not in captured["context"]
    assert captured["context"]["emotional_direction"]["pov_arc_step"] == "第一次承担风险"


def test_outline_quality_accepts_short_volume_title_and_never_blocks_first_draft(client, monkeypatch):
    work = {
        "title": "质量提示测试",
        "story_bible": {"summary": "完整梗概", "reader_promise": "明确读者承诺"},
        "characters": [{"name": "陈野"}, {"name": "苏晚晴"}],
        "plot_arcs": [{"title": "第1卷"}],
    }
    chapter = {
        "chapter_no": 1, "title": "铁门升起", "pov_character": "陈野", "goal": "守住地下室并完成铁门升级。",
        "conflict": "丧尸撞门与材料不足同时逼近。", "failure_cost": "地下室失守并失去庇护所。",
        "beats": ["断电后冲进地下室", "清点材料发现不足", "邻居求救引来丧尸", "升级铁门挡住冲击", "留下新的资源问题"],
        "hook": "门外出现新的求救信号。", "plot_arc": "第1卷",
        "opening_state": {"location": "地下室"}, "ending_state": {"new_problem": "材料不足"},
        "causal_beats": [{"cause": "断电", "action": "进入地下室", "obstacle": "丧尸撞门", "consequence": "决定升级铁门"}],
        "time_mode": "linear", "story_day": 0, "phase_key": "default", "appearing_characters": ["陈野"],
        "task_progress": [{"task": "守住地下室", "progress": "完成铁门升级"}],
        "title_promise_progress": "首次兑现庇护所升级承诺。", "character_arc_progress": "陈野首次主动承担救人的风险。",
    }
    issues, _score = evaluate_outline(work, [chapter], 1)
    assert not any("所属主线" in item for item in issues)

    incomplete = {"chapters": [{**chapter, "title": ""}]}
    novel = NovelEngine()
    monkeypatch.setattr(novel, "_llm_json", lambda *_args, **_kwargs: incomplete)
    draft = novel.generate_outline(work, 1, generation_context={"from_chapter": 1, "to_chapter": 1, "total_target_chapters": 40})
    assert draft["chapters"][0]["chapter_no"] == 1
    assert any("标题" in item for item in draft["quality_issues"])


def test_proxy_settings_are_persisted_and_applied_to_codex(client):
    initial = client.get("/api/settings/proxy")
    assert initial.status_code == 200
    saved = client.put("/api/settings/proxy", json={"enabled": True, "port": 23456})
    assert saved.status_code == 200
    assert saved.json()["enabled"] is True
    assert saved.json()["port"] == 23456
    environment = codex_process_env()
    assert environment["HTTP_PROXY"] == "http://127.0.0.1:23456"
    assert environment["HTTPS_PROXY"] == "http://127.0.0.1:23456"
    assert environment["ALL_PROXY"] == "http://127.0.0.1:23456"
    assert client.put("/api/settings/proxy", json={"enabled": False, "port": 10808}).status_code == 200


def test_prompt_settings_can_be_listed_updated_used_and_restored(client):
    initial = client.get("/api/settings/prompts")
    assert initial.status_code == 200
    items = initial.json()["items"]
    assert {item["key"] for item in items} == {
        "planning", "setup", "setup_repair", "outline", "outline_repair",
        "volume_outline", "chapter", "editor", "extraction", "trend",
    }
    chapter = next(item for item in items if item["key"] == "chapter")
    assert chapter["content"] == chapter["default_content"]
    assert chapter["is_customized"] is False

    custom = "你是测试用章节作者。严格遵守已确认事实。"
    saved = client.put("/api/settings/prompts/chapter", json={"content": custom})
    assert saved.status_code == 200
    assert saved.json()["content"] == custom
    assert saved.json()["is_customized"] is True
    assert configured_prompt("chapter") == custom

    restored = client.delete("/api/settings/prompts/chapter")
    assert restored.status_code == 200
    assert restored.json()["is_customized"] is False
    assert restored.json()["content"] == chapter["default_content"]
    assert configured_prompt("chapter") == chapter["default_content"]
    assert client.put("/api/settings/prompts/not-a-stage", json={"content": "x"}).status_code == 404


def test_trend_refresh_returns_persisted_item_ids(client, monkeypatch):
    class FakeResponse:
        text = '<a href="/book/123456/">测试热门作品</a>'

        def raise_for_status(self):
            return None

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def get(self, _url):
            return FakeResponse()

    monkeypatch.setattr(trends.httpx, "Client", lambda **_kwargs: FakeClient())
    category = f"test-{uuid4().hex}"
    fresh = trends.refresh_source("qidian", category=category, force=True)
    assert fresh["items"]
    assert fresh["items"][0]["id"]
    cached = trends.search_trends(["qidian"], category=category, refresh=False)
    assert cached["items"][0]["id"] == fresh["items"][0]["id"]


def test_trend_adapters_recover_jjwxc_gb18030_and_qidian_info_urls(client, monkeypatch):
    class FakeResponse:
        content = b'<a href="/onebook.php?novelid=1">\xc4\xe3\xba\xc3</a>'
        text = '<a href="/onebook.php?novelid=1">\ufffd\ufffd\u013a</a>'

        def raise_for_status(self):
            return None

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def get(self, _url):
            return FakeResponse()

    monkeypatch.setattr(trends.httpx, "Client", lambda **_kwargs: FakeClient())
    jjwxc = trends.refresh_source("jjwxc", category=f"encoding-{uuid4().hex}", force=True)
    assert jjwxc["items"][0]["title"] == "你好"
    assert trends._extract_links('<a href="//book.qidian.com/info/123/">测试书</a>', "qidian")[0]["source_url"] == "https://book.qidian.com/info/123/"


def test_fanqie_adapter_uses_public_rank_api_and_plain_detail_metadata(client, monkeypatch):
    class FakeResponse:
        def __init__(self, *, payload=None, text=""):
            self._payload = payload
            self.text = text

        def json(self):
            return self._payload

        def raise_for_status(self):
            return None

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def get(self, url, **kwargs):
            if url == trends.FANQIE_CATEGORY_CONFIG_URL:
                assert kwargs["params"] == {"config_key": "serial_rank_category_list_common"}
                return FakeResponse(payload={"data": {"list": [{"id": "1141", "name": "西方奇幻", "group": ["male"]}]}})
            if url == trends.FANQIE_RANK_API_URL:
                assert kwargs["params"]["category_id"] == "1141"
                return FakeResponse(payload={"code": 0, "data": {"book_list": [
                    {"bookId": "123", "currentPos": 1, "read_count": "456789", "bookName": "\ue001\ue002"},
                ]}})
            if url == "https://fanqienovel.com/page/123":
                return FakeResponse(text=(
                    "<title>苍穹远征完整版在线免费阅读_苍穹远征小说_番茄小说官网</title>"
                    '<meta name="description" content="番茄小说提供苍穹远征完整版在线免费阅读，精彩小说尽在番茄小说网。少年在边境发现一座失落星门。">'
                ))
            raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr(trends.httpx, "Client", lambda **_kwargs: FakeClient())
    result = trends.refresh_source("fanqie", category="西方奇幻", force=True)
    item = result["items"][0]
    assert item["title"] == "苍穹远征"
    assert item["synopsis"] == "少年在边境发现一座失落星门。"
    assert item["board"] == "男频阅读榜"
    assert item["metric_label"] == "在读"
    assert item["source_url"] == "https://fanqienovel.com/page/123"


def test_qidian_mobile_adapter_extracts_structured_ranking_cards():
    html = '''
        <a href="//m.qidian.com/book/1040765595/" title="夜无疆最新章节在线阅读" class="bookItem">
          <div class="ranking">1</div><h2>夜无疆</h2>
          <p class="bookDesc">那一天太阳落下再也没有升起。</p>
          <p class="subTitle">辰东 <em>·</em> 玄幻 <em>·</em> 389.75万字</p>
        </a>
    '''
    items = trends._parse_source("qidian", html)
    assert items == [
        {
            "source": "qidian", "source_id": "1040765595", "rank": 1, "board": "畅销榜", "category": "玄幻",
            "title": "夜无疆", "author": "辰东", "synopsis": "那一天太阳落下再也没有升起。",
            "metric_label": "畅销排名", "metric_value": "1", "source_url": "https://m.qidian.com/book/1040765595/",
            "captured_at": items[0]["captured_at"],
        }
    ]


def test_qidian_waf_response_is_reported_as_security_challenge(client, monkeypatch):
    class FakeResponse:
        status_code = 202
        headers = {"X-WAF-UUID": "test"}
        text = '<script src="/C2WF946J0/probe.js"></script>'

        def raise_for_status(self):
            return None

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def get(self, _url):
            return FakeResponse()

    monkeypatch.setattr(trends.httpx, "Client", lambda **_kwargs: FakeClient())
    with pytest.raises(ValueError, match="安全验证"):
        trends.refresh_source("qidian", category=f"waf-{uuid4().hex}", force=True)


def test_trend_search_does_not_label_a_source_error_as_offline_cache(monkeypatch):
    def fail(*_args, **_kwargs):
        raise ValueError("source unavailable")

    monkeypatch.setattr(trends, "refresh_source", fail)
    result = trends.search_trends(["qidian"], refresh=True)
    assert result["items"] == []
    assert result["sources"] == [{"source": "qidian", "stale": False, "error": "source unavailable"}]


def test_trend_blueprint_persists_sources_and_reaches_planning_context(client, monkeypatch):
    now = now_iso()
    snapshot_id, item_id = str(uuid4()), str(uuid4())
    with transaction() as conn:
        conn.execute(
            "INSERT INTO trend_snapshots(id,source,category,captured_at,expires_at,stale,error,created_at) VALUES (?,?,?,?,?,?,?,?)",
            (snapshot_id, "qidian", "integration-test", now, "2099-01-01T00:00:00+00:00", 0, "", now),
        )
        conn.execute(
            """INSERT INTO trend_items(id,snapshot_id,source,source_id,rank,board,category,title,author,synopsis,metric_label,metric_value,source_url,captured_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (item_id, snapshot_id, "qidian", f"source-{item_id}", 1, "综合榜", "科幻", "来源作品", "", "官方简介摘要", "排名", "1", "https://example.com/book", now),
        )

    def fake_generate(items, _profile):
        return {
            "trend_summary": "强目标与阶段性回报有效。",
            "rising_themes": ["资源竞争"],
            "overcrowded_directions": ["换名套壳"],
            "source_models": [{
                "trend_item_id": items[0]["id"], "completeness": "medium",
                "model": {"market_positioning": "强目标类型读者", "narrative_engine": {"opening": "资源失衡", "protagonist": "受限行动者", "conflict": "规则压迫", "stakes": "失去行动自由"}, "serial_engine": {"payoff_cadence": "短周期回报", "hook_types": ["反转"]}, "safe_signals": ["明确目标"], "avoid_copying": ["不复用专有词"]},
            }],
            "ideas": [{
                "title": "雾港登记员", "genre": "近未来悬疑", "audience": "长篇连载读者", "hook": "登记员发现每份合法通行证都会抹去一段记忆。", "premise": "主角必须在港口封锁前找回被抹去的证据。", "synopsis": "一名底层登记员对抗会改写身份记录的系统。", "differentiation": "以记忆凭证替代资源升级。", "risk": "核查术语和事件链。",
                "blueprint": {"market_signals": ["明确目标", "短周期回报"], "creative_direction": "以身份登记系统制造新冲突。", "transformation_contract": {"retain": ["目标驱动"], "change": ["世界规则", "主角身份", "核心机制", "关系结构", "事件链", "结局"], "entity_rules": {"characters": "重新命名与重写经历", "places": "重新设计", "items": "重新设计用途与代价"}, "avoid": ["不得换名套壳", "不得复用专有词"]}, "story_seed": {"hook": "登记记录异常", "premise": "主角找回证据", "reader_promise": "通过主动调查取得阶段回报"}},
            }],
        }

    monkeypatch.setattr(trends.engine, "generate_trend_ideas", fake_generate)
    profile = client.post("/api/model-profiles", json={
        "name": f"趋势测试-{uuid4().hex}", "base_url": "https://example.com/v1", "model": "test-model", "api_key": "sk-trend-test", "is_default": True,
    })
    assert profile.status_code == 200
    analysis = client.post("/api/trends/analyze", json={"item_ids": [item_id], "model_profile_id": profile.json()["id"]})
    assert analysis.status_code == 200, analysis.text
    data = analysis.json()
    assert data["items"][0]["id"] == item_id
    assert data["source_models"][0]["completeness"] == "medium"
    blueprint_id = data["ideas"][0]["blueprint_id"]

    created = client.post("/api/works/from-inspiration-blueprint", json={"blueprint_id": blueprint_id, "model_profile_id": profile.json()["id"], "idempotency_key": f"create-{blueprint_id}"})
    assert created.status_code == 200, created.text
    work = created.json()
    assert work["inspiration_sources"][0]["title"] == "来源作品"
    assert work["inspiration_blueprint"]["content"]["creative_brief"]["market_signals"] == ["明确目标", "短周期回报"]
    duplicate = client.post("/api/works/from-inspiration-blueprint", json={"blueprint_id": blueprint_id, "idempotency_key": f"create-{blueprint_id}"})
    assert duplicate.status_code == 200
    assert duplicate.json()["id"] == work["id"]
    context = _planning_context(get_work(work["id"]))
    assert context["inspiration_brief"]["transformation_contract"]["change"]
    assert client.patch(f"/api/model-profiles/{profile.json()['id']}", json={"enabled": False}).status_code == 200


def test_delete_work(client):
    created = client.post("/api/works", json={"title": "待删除作品", "premise": "用于验证删除流程。"})
    assert created.status_code == 200
    work_id = created.json()["id"]

    deleted = client.delete(f"/api/works/{work_id}")
    assert deleted.status_code == 200
    assert deleted.json() == {"ok": True, "id": work_id}
    assert client.get(f"/api/works/{work_id}").status_code == 404
    assert all(item["id"] != work_id for item in client.get("/api/works").json()["items"])


def test_full_novel_mvp_flow(client):
    created = client.post(
        "/api/works",
        json={
            "title": "潮汐之后",
            "genre": "都市悬疑",
            "target_audience": "长篇连载",
            "estimated_words": 100000,
            "writing_style": "节奏清晰，场景具体",
            "premise": "一名普通记者发现旧案与自己有关。",
        },
    )
    assert created.status_code == 200
    work_id = created.json()["id"]

    setup = client.post(f"/api/works/{work_id}/generate/setup")
    assert setup.status_code == 200
    assert setup.json()["work"]["story_bible"]["summary"]
    assert len(setup.json()["work"]["characters"]) >= 2

    outline = client.post(f"/api/works/{work_id}/generate/outline", json={"chapter_count": 3})
    assert outline.status_code == 200
    assert len(outline.json()["work"]["chapter_plans"]) == 3

    chapter = client.post(f"/api/works/{work_id}/generate/chapter", json={"chapter_no": 1, "mode": "chapter"})
    assert chapter.status_code == 200
    assert chapter.json()["data"]["content"]
    assert chapter.json()["quality"]["score"] >= 0
    queued_extraction = chapter.json()["state_extraction"]
    assert queued_extraction["status"] == "queued"
    assert queued_extraction["chapter_version_id"]
    assert run_worker_once() is True
    extraction = client.get(f"/api/works/{work_id}/state-extractions").json()["items"][0]
    assert extraction["status"] == "pending"
    assert len(extraction["timeline_events"]) == 1

    event_id = extraction["timeline_events"][0]["id"]
    reviewed = client.post(
        f"/api/works/{work_id}/state-extractions/{extraction['id']}/review",
        json={"items": [{"id": event_id, "kind": "timeline", "action": "accept"}]},
    )
    assert reviewed.status_code == 200
    assert reviewed.json()["status"] == "applied"
    assert reviewed.json()["timeline_events"][0]["review_status"] == "confirmed"

    rerun = client.post(f"/api/works/{work_id}/chapters/1/extract-state")
    assert rerun.status_code == 200
    assert rerun.json()["status"] == "queued"
    assert run_worker_once() is True
    rerun_extraction = client.get(f"/api/works/{work_id}/state-extractions").json()["items"][0]
    assert rerun_extraction["id"] != extraction["id"]
    old = client.get(f"/api/works/{work_id}/state-extractions/{extraction['id']}")
    assert old.json()["status"] == "superseded"

    pending = client.get(f"/api/works/{work_id}/state-extractions?status=pending")
    assert pending.status_code == 200
    assert len(pending.json()["items"]) == 1

    extraction_detail = client.get(f"/api/works/{work_id}/state-extractions/{extraction['id']}")
    assert extraction_detail.status_code == 200
    assert extraction_detail.json()["id"] == extraction["id"]

    saved = client.patch(
        f"/api/works/{work_id}/chapters/1",
        json={"content": "主角走进旧档案室。关键对手已经在那里等他。"},
    )
    assert saved.status_code == 200
    assert saved.json()["work"]["chapters"][0]["content"].startswith("主角")
    assert saved.json()["state_extraction"]["status"] == "queued"
    assert run_worker_once() is True

    detail = client.get(f"/api/works/{work_id}")
    assert detail.status_code == 200
    assert detail.json()["chapters"][0]["chapter_no"] == 1

    # 自动提取只产生待审核候选，正式角色状态不应被静默修改。
    conn_check = client.get(f"/api/works/{work_id}/state-extractions").json()
    assert len(conn_check["items"]) == 3


def test_reviewed_character_state_enters_context(client, monkeypatch):
    created = client.post("/api/works", json={"title": "状态测试", "premise": "主角寻找失踪的朋友。"})
    work_id = created.json()["id"]
    assert client.post(f"/api/works/{work_id}/generate/setup").status_code == 200
    character_name = client.get(f"/api/works/{work_id}").json()["characters"][0]["name"]

    def fake_extract(work, chapter, profile=None):
        return {
            "characters": [{
                "character_name": character_name,
                "aliases": [],
                "changes": [{
                    "field": "location",
                    "old_value": None,
                    "new_value": "旧档案室",
                    "evidence": "主角走进旧档案室。",
                    "confidence": 0.95,
                }],
            }],
            "timeline_events": [],
            "warnings": [],
        }

    monkeypatch.setattr(engine, "extract_state_changes", fake_extract)
    chapter = client.post(
        f"/api/works/{work_id}/generate/chapter",
        json={"chapter_no": 1, "mode": "chapter"},
    )
    assert chapter.status_code == 200
    assert chapter.json()["state_extraction"]["status"] == "queued"
    assert run_worker_once() is True
    extraction = client.get(f"/api/works/{work_id}/state-extractions").json()["items"][0]
    change = extraction["characters"][0]
    reviewed = client.post(
        f"/api/works/{work_id}/state-extractions/{extraction['id']}/review",
        json={"items": [{"id": change["id"], "kind": "character", "action": "accept"}]},
    )
    assert reviewed.status_code == 200
    detail = client.get(f"/api/works/{work_id}").json()
    assert detail["character_states"][0]["state"]["location"] == "旧档案室"
    context = build_context(detail, 2)
    assert context["characters"][0]["confirmed_state"]["location"] == "旧档案室"


def test_setup_is_title_bound_and_contains_character_biographies(client):
    created = client.post("/api/works", json={"title": "潮汐之后", "genre": "都市悬疑"})
    work_id = created.json()["id"]
    response = client.post(f"/api/works/{work_id}/generate/setup")
    assert response.status_code == 200
    work = response.json()["work"]
    bible = work["story_bible"]
    assert "潮汐之后" in bible["title_interpretation"]
    assert bible["reader_promise"]
    assert len(bible["must_have_elements"]) >= 3
    assert bible["quality_score"] >= 70
    assert len(work["characters"]) >= 3
    assert all(len(item["biography"]) >= 60 for item in work["characters"])
    assert all(item["character_arc"] and item["voice"] for item in work["characters"])


def test_outline_requires_setup_and_replaces_stale_plans(client):
    created = client.post("/api/works", json={"title": "回声失物招领处"})
    work_id = created.json()["id"]
    missing_setup = client.post(f"/api/works/{work_id}/generate/outline", json={"chapter_count": 3})
    assert missing_setup.status_code >= 400

    assert client.post(f"/api/works/{work_id}/generate/setup").status_code == 200
    first = client.post(f"/api/works/{work_id}/generate/outline", json={"chapter_count": 5})
    assert first.status_code == 200
    assert len(first.json()["work"]["chapter_plans"]) == 5
    second = client.post(f"/api/works/{work_id}/generate/outline", json={"chapter_count": 3})
    assert second.status_code == 200
    plans = second.json()["work"]["chapter_plans"]
    assert len(plans) == 3
    assert all(item["title_promise_progress"] and item["character_arc_progress"] for item in plans)


def test_versioned_story_time_blocks_early_factions_and_invalidates_downstream(client):
    created = client.post("/api/works", json={"title": "时间状态引擎"})
    work_id = created.json()["id"]
    assert client.post(f"/api/works/{work_id}/generate/setup").status_code == 200
    assert client.post(f"/api/works/{work_id}/generate/outline", json={"chapter_count": 2}).status_code == 200

    phase = client.put(f"/api/works/{work_id}/story-phases/ordinary", json={
        "phase_key": "ordinary", "name": "普通世界", "start_day": -30, "end_day": -1,
        "rules": ["社会秩序仍然存在"], "locked": True,
    })
    assert phase.status_code == 200
    faction = client.put(f"/api/works/{work_id}/factions/铁牙帮", json={
        "name": "铁牙帮", "formed_day": 5, "lifecycle": "planned",
    })
    assert faction.status_code == 200

    valid = client.put(f"/api/works/{work_id}/chapter-plans/1", json={
        "story_day": -30, "phase_key": "ordinary", "title": "重生与合同",
        "goal": "在普通世界中完成庇护所签约", "conflict": "资金和工期不足", "beats": ["签约"], "hook": "开始采购",
    })
    assert valid.status_code == 200, valid.text
    early = client.put(f"/api/works/{work_id}/chapter-plans/1", json={"goal": "与铁牙帮巡逻队冲突"})
    assert early.status_code == 409
    assert "铁牙帮" in early.json()["detail"]

    saved = client.patch(f"/api/works/{work_id}/chapters/1", json={"title": "重生与合同", "content": "陈烈签下合同。"})
    assert saved.status_code == 200
    # Saving prose queues a state-extraction job; drain it so this test leaves no shared worker work behind.
    assert run_worker_once() is True
    revised = client.put(f"/api/works/{work_id}/chapter-plans/1", json={"hook": "采购清单突然涨价"})
    assert revised.status_code == 200
    work = revised.json()
    assert work["chapters"][0]["stale_reason"]
    assert work["chapter_plans"][1]["stale_reason"]


def test_formal_character_card_can_be_edited_and_marks_related_assets_for_review(client):
    created = client.post("/api/works", json={"title": "人物卡可编辑"})
    work_id = created.json()["id"]
    assert client.post(f"/api/works/{work_id}/generate/setup").status_code == 200
    assert client.post(f"/api/works/{work_id}/generate/outline", json={"chapter_count": 2}).status_code == 200
    work = client.get(f"/api/works/{work_id}").json()
    character = work["characters"][0]
    assert client.put(
        f"/api/works/{work_id}/chapter-plans/1",
        json={"pov_character": character["name"], "appearing_characters": [character["name"]]},
    ).status_code == 200
    assert client.patch(
        f"/api/works/{work_id}/chapters/1", json={"title": "第一章", "content": "人物登场。"}
    ).status_code == 200
    assert run_worker_once() is True

    changed = client.put(
        f"/api/works/{work_id}/characters/{character['id']}",
        json={"name": character["name"], "biography": "这是作者修改后的正式人物小传，后续章节应以此为准。"},
    )
    assert changed.status_code == 200, changed.text
    body = changed.json()
    assert body["impact"] == {"affected_plan_count": 2, "affected_chapter_count": 1}
    updated = body["work"]
    assert updated["characters"][0]["biography"].startswith("这是作者修改后的正式人物小传")
    assert updated["chapter_plans"][0]["stale_reason"]
    assert updated["chapter_plans"][1]["stale_reason"]
    assert updated["chapters"][0]["stale_reason"]


def test_persistent_generation_job_is_idempotent_and_runnable(client):
    created = client.post("/api/works", json={"title": "任务测试"})
    work_id = created.json()["id"]
    payload = {"kind": "setup", "idempotency_key": "setup-once"}
    first = client.post(f"/api/works/{work_id}/generation-jobs", json=payload)
    second = client.post(f"/api/works/{work_id}/generation-jobs", json=payload)
    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()["id"] == second.json()["id"]
    assert run_worker_once() is True
    completed = client.get(f"/api/works/{work_id}/generation-jobs/{first.json()['id']}")
    assert completed.json()["status"] == "completed"
    assert client.get(f"/api/works/{work_id}").json()["story_bible"]["summary"]

    failed = client.post(
        f"/api/works/{work_id}/generation-jobs",
        json={"kind": "state_extraction", "payload": {"chapter_no": 99}},
    )
    assert failed.status_code == 202
    assert run_worker_once() is True
    failed_detail = client.get(f"/api/works/{work_id}/generation-jobs/{failed.json()['id']}")
    assert failed_detail.json()["status"] == "failed"
    retried = client.post(f"/api/works/{work_id}/generation-jobs/{failed.json()['id']}/retry")
    assert retried.status_code == 200
    assert retried.json()["status"] == "queued"


def test_volume_outline_job_keeps_dynamic_coordinates_and_returns_unsaved_draft(client, monkeypatch):
    created = client.post(
        "/api/works",
        json={"title": "固定卷范围", "estimated_words": 100000, "premise": "主角在末日第二天建立临时庇护所。"},
    )
    work_id = created.json()["id"]
    assert client.post(f"/api/works/{work_id}/generate/setup").status_code == 200
    structure = client.post(f"/api/works/{work_id}/narrative-structure/bootstrap", json={"replace": False})
    assert structure.status_code == 200
    before = structure.json()
    volume = before["story_volumes"][0]
    stages = [item for item in before["narrative_stages"] if item["volume_id"] == volume["id"]]
    # The number of volumes comes from confirmed plot arcs, so this test must
    # read the actual range instead of assuming the first volume is always 40.
    # It only needs to prove that a range larger than the 12-chapter batch is
    # preserved even when the model attempts to return 1—12.
    assert volume["end_chapter"] - volume["start_chapter"] + 1 > 12
    original_synopsis = volume["synopsis"]
    original_purpose = stages[0]["purpose"]
    # Reproduce the historical bug: a short chapter batch was written as the
    # book target.  Generating a volume draft must restore the authored volume
    # lower bound rather than rebuilding it as a 12-chapter volume.
    with transaction() as conn:
        conn.execute("UPDATE works SET target_chapter_count=12 WHERE id=?", (work_id,))

    def fake_volume_response(_system, user, *_args, **_kwargs):
        request = json.loads(user)
        generated_stages = [{
            "id": item["id"], "sequence": item["sequence"],
            # These coordinates are malicious/incorrect on purpose: the engine
            # must take the authored values instead of trusting the response.
            "start_chapter": 1, "end_chapter": 12,
            "title": f"模型阶段{item['sequence']}", "purpose": "推进当前卷的具体任务。",
            "entry_state": {"summary": "承接上一个阶段的具体局面。"},
            "exit_state": {"summary": "形成下一阶段可承接的具体变化。"},
            "allowed_payoffs": ["获得局部资源。"],
            "forbidden_payoffs": ["不得提前完成卷末清算。"],
            "prerequisites": [],
        } for item in request["fixed_stage_coordinates"]]
        return ({
            "volume": {
                "title": "模型卷名", "synopsis": "完整覆盖当前固定分卷范围的卷级推进。",
                "goal": "在卷末完成阶段目标。", "opposition": "只使用当前阶段允许登场的阻力。",
                "ending_state": {"summary": "卷末形成可继续推进的新局面。"},
            },
            "stages": generated_stages,
        }, {"input_tokens": 11, "output_tokens": 22, "total_tokens": 33})

    monkeypatch.setattr(engine, "_llm_json_with_usage", fake_volume_response)
    queued = client.post(
        f"/api/works/{work_id}/generation-jobs",
        json={"kind": "volume_outline", "payload": {"volume_id": volume["id"]}},
    )
    assert queued.status_code == 202, queued.text
    assert run_worker_once() is True
    completed = client.get(f"/api/works/{work_id}/generation-jobs/{queued.json()['id']}").json()
    assert completed["status"] == "completed"
    data = completed["output"]["data"]
    assert data["generation_source"] == "model"
    assert data["quality_ok"] is True
    assert data["volume"]["start_chapter"] == volume["start_chapter"]
    assert data["volume"]["end_chapter"] == volume["end_chapter"]
    assert [(item["id"], item["start_chapter"], item["end_chapter"]) for item in data["stages"]] == [
        (item["id"], item["start_chapter"], item["end_chapter"]) for item in stages
    ]

    # The completed job holds a review draft only; no volume or stage was saved.
    after = client.get(f"/api/works/{work_id}").json()
    saved_volume = next(item for item in after["story_volumes"] if item["id"] == volume["id"])
    saved_stage = next(item for item in after["narrative_stages"] if item["id"] == stages[0]["id"])
    assert after["target_chapter_count"] >= volume["end_chapter"]
    assert saved_volume["synopsis"] == original_synopsis
    assert saved_stage["purpose"] == original_purpose


def test_volume_outline_returns_partial_stage_draft_with_failure_reason(client, monkeypatch):
    created = client.post("/api/works", json={"title": "局部卷纲失败", "premise": "主角必须在限时内守住临时据点。"})
    work_id = created.json()["id"]
    assert client.post(f"/api/works/{work_id}/generate/setup").status_code == 200
    structure = client.post(f"/api/works/{work_id}/narrative-structure/bootstrap", json={"replace": False}).json()
    volume = structure["story_volumes"][0]
    stage = next(item for item in structure["narrative_stages"] if item["volume_id"] == volume["id"])

    def incomplete_stage_response(*_args, **_kwargs):
        # The model omits title and several required fields.  This must produce
        # a completed review draft, not a failed job that hides all content.
        return ({"stage": {
            "id": stage["id"], "sequence": stage["sequence"], "purpose": "仅返回了阶段任务。",
            "start_chapter": 1, "end_chapter": 12,
        }}, {})

    monkeypatch.setattr(engine, "_llm_json_with_usage", incomplete_stage_response)
    queued = client.post(
        f"/api/works/{work_id}/generation-jobs",
        json={"kind": "volume_outline", "payload": {"volume_id": volume["id"], "target_stage_id": stage["id"]}},
    )
    assert queued.status_code == 202
    assert run_worker_once() is True
    completed = client.get(f"/api/works/{work_id}/generation-jobs/{queued.json()['id']}").json()
    assert completed["status"] == "completed"
    data = completed["output"]["data"]
    assert data["quality_ok"] is False
    assert data["stages"][0]["id"] == stage["id"]
    assert data["stages"][0]["title"] == stage["title"]
    assert any(item["scope"] == f"stage:{stage['id']}" for item in data["quality_issues"])


def test_model_profile_and_foreshadow_workflows(client):
    profile = client.post("/api/model-profiles", json={
        "name": "自定义测试模型",
        "base_url": "https://example.com/v1",
        "model": "test-model",
        "api_key": "sk-test-123456",
        "is_default": True,
    })
    assert profile.status_code == 200
    assert profile.json()["has_api_key"] is True
    assert "sk-test" not in profile.text

    codex_profile = client.post("/api/model-profiles", json={
        "name": "本机 Codex Auth",
        "provider": "codex_auth",
        "base_url": "codex://local",
        "model": "gpt-5.6-sol",
    })
    assert codex_profile.status_code == 200
    assert codex_profile.json()["provider"] == "codex_auth"
    assert codex_profile.json()["has_api_key"] is False

    work = client.post("/api/works", json={"title": "伏笔工作流", "model_profile_id": profile.json()["id"]})
    work_id = work.json()["id"]
    item = client.post(f"/api/works/{work_id}/foreshadows", json={
        "clue": "旧车站的钥匙",
        "planted_chapter": 1,
        "expected_reveal_chapter": 3,
    })
    assert item.status_code == 200
    assert client.get(f"/api/works/{work_id}/foreshadows").json()["items"][0]["clue"] == "旧车站的钥匙"
    updated = client.patch(f"/api/works/{work_id}/foreshadows/{item.json()['id']}", json={"status": "revealed", "actual_reveal_chapter": 4})
    assert updated.status_code == 200
    assert updated.json()["status"] == "revealed"
    # Do not let fake credentials from this storage test affect later generation tests.
    assert client.patch(f"/api/model-profiles/{profile.json()['id']}", json={"enabled": False}).status_code == 200
    assert client.patch(f"/api/model-profiles/{codex_profile.json()['id']}", json={"enabled": False}).status_code == 200


def test_codex_connection_test_has_a_short_timeout(client, monkeypatch):
    profile = client.post("/api/model-profiles", json={
        "name": f"Codex 连接测试-{uuid4().hex}",
        "provider": "codex_auth",
        "base_url": "codex://local",
        "model": "gpt-5.6-sol",
        "timeout_seconds": 600,
    })
    assert profile.status_code == 200
    seen: dict[str, float] = {}
    monkeypatch.setattr(model_profiles, "codex_auth_status", lambda: {"ok": True, "message": "已登录"})
    monkeypatch.setattr(engine, "probe_codex_auth", lambda value: seen.update(timeout_seconds=value["timeout_seconds"]))

    response = client.post(f"/api/model-profiles/{profile.json()['id']}/test")

    assert response.status_code == 200, response.text
    assert seen["timeout_seconds"] == model_profiles.CODEX_CONNECTION_TEST_TIMEOUT_SECONDS


def test_codex_exec_explicitly_reads_prompt_from_stdin(monkeypatch):
    captured: dict[str, object] = {}

    class FakeStdin:
        def __init__(self):
            self.value = ""

        def write(self, value):
            self.value += value

        def close(self):
            captured["stdin"] = self.value

    class FakeProcess:
        def __init__(self, args, **_kwargs):
            captured["args"] = args
            self.stdin = FakeStdin()
            self.returncode = 0
            self.pid = 123
            output_path = args[args.index("--output-last-message") + 1]
            Path(output_path).write_text('{"ok": true}', encoding="utf-8")

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(novel_engine.shutil, "which", lambda _name: "codex")
    monkeypatch.setattr(novel_engine.subprocess, "Popen", FakeProcess)
    monkeypatch.setattr(novel_engine, "codex_process_env", lambda: {})
    monkeypatch.setattr(novel_engine, "start_model_call", lambda *_args, **_kwargs: "call-id")
    monkeypatch.setattr(novel_engine, "finish_model_call", lambda *_args, **_kwargs: None)

    result = NovelEngine()._codex_json("system", "user", {"model": "gpt-5.6-sol"})

    assert result == {"ok": True}
    assert captured["args"][-1] == "-"
    assert captured["stdin"] == "system\n只输出合法 JSON，不要 Markdown 代码块，不要解释。\nuser"


def test_generation_job_exposes_and_freezes_actual_model(client):
    profile = client.post("/api/model-profiles", json={
        "name": "任务路由测试模型",
        "provider": "deepseek",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-v4-flash",
        "api_key": "sk-route-test",
    })
    assert profile.status_code == 200
    work = client.post("/api/works", json={"title": "路由可见性", "model_profile_id": profile.json()["id"]})
    work_id = work.json()["id"]

    queued = client.post(
        f"/api/works/{work_id}/generation-jobs",
        json={"kind": "planning_step", "payload": {"step": "contract", "item_key": "contract"}},
    )
    assert queued.status_code == 202
    assert queued.json()["model_profile_id"] == profile.json()["id"]
    assert queued.json()["resolved_provider"] == "deepseek"
    assert queued.json()["resolved_model"] == "deepseek-v4-flash"
    canceled = client.post(f"/api/works/{work_id}/generation-jobs/{queued.json()['id']}/cancel")
    assert canceled.status_code == 200
    assert canceled.json()["status"] == "canceled"

    invalid = client.post(
        f"/api/works/{work_id}/generation-jobs",
        json={"kind": "planning_step", "model_profile_id": "missing-profile", "payload": {"step": "contract"}},
    )
    assert invalid.status_code == 422
    assert client.patch(f"/api/model-profiles/{profile.json()['id']}", json={"enabled": False}).status_code == 200


def test_staged_planning_requires_confirmation_and_finalizes(client):
    created = client.post("/api/works", json={"title": "分阶段测试", "genre": "类型小说"})
    assert created.status_code == 200
    work_id = created.json()["id"]

    session = client.get(f"/api/works/{work_id}/planning-session")
    assert session.status_code == 200
    assert session.json()["current_step"] == "contract"

    setting_before_contract = client.post(f"/api/works/{work_id}/planning-steps/setting/generate", json={})
    assert setting_before_contract.status_code == 409

    def generate(step, item_key="default", feedback="", request_key=None):
        payload = {"item_key": item_key, "feedback": feedback, "preset": "extreme爽文"}
        if request_key:
            payload["idempotency_key"] = request_key
        response = client.post(
            f"/api/works/{work_id}/planning-steps/{step}/generate",
            json=payload,
        )
        assert response.status_code == 202, response.text
        job_id = response.json()["id"]
        for _ in range(12):
            run_worker_once()
            detail = client.get(f"/api/works/{work_id}/generation-jobs/{job_id}")
            if detail.json()["status"] in {"completed", "failed", "canceled"}:
                break
        assert detail.json()["status"] == "completed", detail.text
        return detail.json()

    contract = generate("contract", "contract")
    assert len(contract["output"]["data"]["candidates"]) == 3
    assert client.post(f"/api/works/{work_id}/planning-steps/contract/contract/confirm", json={"candidate_index": 0}).status_code == 200
    generate("setting")
    assert client.post(f"/api/works/{work_id}/planning-steps/setting/default/confirm", json={}).status_code == 200
    generate("protagonist")
    assert client.post(f"/api/works/{work_id}/planning-steps/protagonist/default/confirm", json={}).status_code == 200
    roster = generate("cast_roster")
    assert len(roster["output"]["data"]["characters"]) >= 1
    assert client.post(f"/api/works/{work_id}/planning-steps/cast_roster/default/confirm", json={}).status_code == 200

    roster_items = client.get(f"/api/works/{work_id}/planning-session").json()["artifacts"]
    roster_content = next(item["content"] for item in roster_items if item["step"] == "cast_roster")
    roster_characters = roster_content["characters"]
    for index, character in enumerate(roster_characters):
        item_key = character["item_key"]
        biography = generate("character", item_key)
        generated_character = biography["output"]["data"]["character"]
        assert generated_character["name"] == character["name"]
        assert generated_character["role"] == character["role"]
        regenerated = generate("character", item_key, request_key=f"regenerate-{item_key}")
        assert regenerated["id"] != biography["id"]
        confirmed_character = client.post(f"/api/works/{work_id}/planning-steps/character/{item_key}/confirm", json={})
        assert confirmed_character.status_code == 200
        expected_step = "arc" if index == len(roster_characters) - 1 else "character"
        assert confirmed_character.json()["current_step"] == expected_step
    generate("arc", "arc:1")
    assert client.post(f"/api/works/{work_id}/planning-steps/arc/arc:1/confirm", json={}).status_code == 200
    generate("summary")
    assert client.post(f"/api/works/{work_id}/planning-steps/summary/default/confirm", json={}).status_code == 200

    finalized = client.post(f"/api/works/{work_id}/planning-session/finalize")
    assert finalized.status_code == 200, finalized.text
    work = finalized.json()["work"]
    assert work["story_bible"]["summary"]
    assert work["characters"]
    assert all(item["appearance"] for item in work["characters"])
    assert all("dramatic_core" in item for item in work["characters"])
    assert work["plot_arcs"]


def test_batch_character_biographies_create_independent_unconfirmed_drafts(client):
    created = client.post("/api/works", json={"title": "批量人物测试", "genre": "类型小说"})
    work_id = created.json()["id"]

    def generate(step, item_key="default"):
        response = client.post(
            f"/api/works/{work_id}/planning-steps/{step}/generate",
            json={"item_key": item_key, "preset": "custom"},
        )
        assert response.status_code == 202, response.text
        job_id = response.json()["id"]
        assert run_worker_once()
        detail = client.get(f"/api/works/{work_id}/generation-jobs/{job_id}")
        assert detail.json()["status"] == "completed", detail.text
        return detail.json()["output"]

    generate("contract", "contract")
    assert client.post(f"/api/works/{work_id}/planning-steps/contract/contract/confirm", json={"candidate_index": 0}).status_code == 200
    generate("setting")
    assert client.post(f"/api/works/{work_id}/planning-steps/setting/default/confirm", json={}).status_code == 200
    generate("protagonist")
    assert client.post(f"/api/works/{work_id}/planning-steps/protagonist/default/confirm", json={}).status_code == 200
    generate("cast_roster")
    assert client.post(f"/api/works/{work_id}/planning-steps/cast_roster/default/confirm", json={}).status_code == 200

    queued = client.post(
        f"/api/works/{work_id}/planning-steps/character/generate-all",
        json={"preset": "custom"},
    )
    assert queued.status_code == 202, queued.text
    assert queued.json()["kind"] == "planning_character_batch"
    assert run_worker_once()
    job = client.get(f"/api/works/{work_id}/generation-jobs/{queued.json()['id']}").json()
    assert job["status"] == "completed", job
    assert job["output"]["kind"] == "planning_character_batch"

    session = client.get(f"/api/works/{work_id}/planning-session").json()
    roster = next(item["content"]["characters"] for item in session["artifacts"] if item["step"] == "cast_roster")
    drafts = [item for item in session["artifacts"] if item["step"] == "character"]
    assert {item["item_key"] for item in drafts} == {item["item_key"] for item in roster}
    assert all(item["status"] == "draft" for item in drafts)
    assert all(item["content"]["character"]["biography"] for item in drafts)
    assert all(item["content"]["character"]["appearance"] for item in drafts)
    assert all("portrayal" not in item["content"]["character"] for item in drafts)

    repeated = client.post(f"/api/works/{work_id}/planning-steps/character/generate-all", json={})
    assert repeated.status_code == 409


def test_planning_reset_is_guarded_by_existing_chapters(client):
    created = client.post("/api/works", json={"title": "重置保护"})
    work_id = created.json()["id"]
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("INSERT INTO chapters(id, work_id, chapter_no, title, content, status, created_at, updated_at) VALUES (?, ?, 1, '第一章', '正文', 'draft', 'now', 'now')", ("reset-chapter", work_id))
        conn.commit()
    blocked = client.post(f"/api/works/{work_id}/planning-session/reset", json={"confirm": True})
    assert blocked.status_code == 409


def test_planning_language_risk_is_explainable():
    risks = language_risks({"text": "这次升级会烧存活率"})
    assert risks and "烧存活率" in risks[0]


def test_planning_character_check_rejects_repeated_biography():
    biography = "他从旧案中走来，因为一次失误开始追查真相，并在压力中重新选择自己的立场。"
    character = {
        "name": "乙", "goal": "找到真相并保护证人", "conflict": "对手持续销毁关键证据",
        "motivation": "弥补过去未能救人的遗憾", "flaw": "总在信息不足时仓促行动",
        "character_arc": "从孤身追查到学会承担共同责任", "secret": "曾暴露过一名重要证人",
        "voice": "只说短句并用行动逼迫对方表态", "biography": biography,
    }
    context = {"character": [{"character": {**character, "name": "甲"}}]}
    checks = planning_checks("character", {"character": character}, context)
    assert any("过于相似" in issue for issue in checks["blocking"])


def test_compact_character_card_projects_legacy_fields_without_losing_facets():
    card = compact_character({
        "name": "唐棠",
        "role": "防卫队长",
        "story_function": "承担庇护所的武力线",
        "biography": "她从冰封后的靶场逃出，带着仅剩的子弹进入庇护所。",
        "dramatic_core": {
            "goal": "守住庇护所大门",
            "motivation": "等待失散的妹妹",
            "flaw": "过度相信自己的判断",
            "conflict": "她的强硬规则与主角的收容原则冲突",
        },
        "portrayal": "黑发束成低马尾，眉眼冷利，站立时腰背笔直。她说话像报靶，句子很短。",
        "arc": "从独来独往到学会与队友协作。",
        "facets": {"romance": "先是利益结盟，后在共同守城中确认感情。"},
    })
    assert card["goal"] == "守住庇护所大门"
    assert card["character_arc"] == card["arc"]
    assert card["appearance"].startswith("黑发")
    assert card["portrayal"].startswith("黑发")
    assert card["personality"] == ""
    assert card["voice"] == ""
    assert card["facets"]["romance"]["content"].startswith("先是利益")
    assert set(planning_character(card)) == {"name", "role", "story_function", "biography", "dramatic_core", "appearance", "personality", "voice", "arc", "secret", "relationships", "facets"}


def test_planning_character_check_rejects_reused_card_fields():
    appearance = "三十岁上下，肩背宽阔，短发贴着额角，旧工装外套的袖口磨白，左眉尾留着一条浅疤。"
    character = {
        "name": "顾衡", "biography": "他从旧案受害者家属的身份开始追查线索，多年后为阻止证据消失再次回到事发地。",
        "appearance": appearance, "personality": appearance,
        "voice": "说话只保留结论，习惯用反问逼对方表态，沉默时会反复摩挲打火机。",
        "dramatic_core": {"goal": "在证据消失前找出责任人", "motivation": "弥补当年无力保护家人的羞耻", "flaw": "信息不足时仍急于行动", "conflict": "对手持续销毁证据并挑拨同盟"},
        "arc": "从孤身追责到愿意为取得真相的方式承担共同责任。",
    }
    checks = planning_checks("character", {"character": character})
    assert any("外貌与性格内容重复" in issue for issue in checks["blocking"])


def test_planning_character_promotes_a_named_candidate_over_empty_template():
    result = {
        "candidates": [{"character": {"name": "陈野", "role": "主角"}}],
        "character": {"name": "", "role": ""},
    }
    assert NovelEngine._result_character(result) == {"name": "陈野", "role": "主角"}


def test_openai_compatible_stream_reports_progress(monkeypatch):
    progress = []

    class Chunk:
        def __init__(self, content, usage=None, reasoning=""):
            self.content = content
            self.usage_metadata = usage or {}
            self.response_metadata = {}
            self.additional_kwargs = {"reasoning_content": reasoning} if reasoning else {}

    class FakeClient:
        def stream(self, messages):
            yield Chunk("", reasoning="先检查结构")
            yield Chunk('{"ok":')
            yield Chunk("true}", {"input_tokens": 8, "output_tokens": 3, "total_tokens": 11})

        def invoke(self, messages):  # pragma: no cover - streaming should succeed
            raise AssertionError("不应降级为非流式调用")

    monkeypatch.setattr("langchain_openai.ChatOpenAI", lambda **kwargs: FakeClient())
    result, usage = NovelEngine()._llm_json_with_usage(
        "system",
        "user",
        {"id": "stream-test", "api_key": "test-key", "base_url": "https://example.com/v1", "model": "fake"},
        0.1,
        on_progress=progress.append,
        is_cancelled=lambda: False,
    )
    assert result == {"ok": True}
    assert usage["total_tokens"] == 11
    assert progress[0].startswith("已发送请求")
    assert any(message.startswith("模型正在流式输出") for message in progress)
    assert progress[-1].startswith("模型输出完成")


def test_non_stream_request_uses_invoke_even_with_progress_callbacks(monkeypatch):
    progress = []

    class Response:
        content = '{"ok":true}'
        usage_metadata = {"input_tokens": 4, "output_tokens": 2, "total_tokens": 6}
        response_metadata = {}

    class FakeClient:
        def stream(self, messages):  # pragma: no cover - non-stream must not call this
            raise AssertionError("章节正文不应走流式调用")

        def invoke(self, messages):
            return Response()

    monkeypatch.setattr("langchain_openai.ChatOpenAI", lambda **kwargs: FakeClient())
    result, usage = NovelEngine()._llm_json_with_usage(
        "system",
        "user",
        {"id": "chapter-non-stream", "api_key": "test-key", "base_url": "https://example.com/v1", "model": "fake"},
        0.1,
        on_progress=progress.append,
        is_cancelled=lambda: False,
        stream=False,
    )
    assert result == {"ok": True}
    assert usage["total_tokens"] == 6
    assert progress == ["模型正在生成完整响应；本次不使用流式传输"]


def test_task_reasoning_policy_overrides_saved_auto_without_mutation():
    original = {"id": "profile", "reasoning_effort": "auto"}
    assert profile_for_task(original, "chapter")["reasoning_effort"] == "high"
    assert profile_for_task(original, "state_extraction")["reasoning_effort"] == "low"
    assert profile_for_task(original, "planning_step")["reasoning_effort"] == "medium"
    assert profile_for_task(original, "setup")["reasoning_effort"] == "medium"
    assert profile_for_task(original, "outline")["reasoning_effort"] == "medium"
    assert original["reasoning_effort"] == "auto"


def test_codex_tasks_do_not_use_legacy_180_second_timeout():
    profile = {"provider": "codex_auth", "timeout_seconds": 180, "reasoning_effort": "auto"}
    effective = profile_for_task(profile, "chapter")
    assert effective["timeout_seconds"] == 600
    assert effective["reasoning_effort"] == "high"
    assert profile["timeout_seconds"] == 180


def test_named_provider_omits_unsupported_reasoning_parameter(monkeypatch):
    captured = {}

    class Response:
        content = '{"ok":true}'
        usage_metadata = {}
        response_metadata = {}

    class FakeClient:
        def invoke(self, messages):
            return Response()

    def fake_client(**kwargs):
        captured.update(kwargs)
        return FakeClient()

    monkeypatch.setattr("langchain_openai.ChatOpenAI", fake_client)
    result, _ = NovelEngine()._llm_json_with_usage(
        "system", "user",
        {"id": "qwen-reasoning-test", "provider": "qwen", "api_key": "test-key", "base_url": "https://example.com/v1", "model": "qwen", "reasoning_effort": "high"},
    )
    assert result == {"ok": True}
    assert "reasoning_effort" not in captured
    assert "extra_body" not in captured


def test_deepseek_high_uses_provider_thinking_extension(monkeypatch):
    captured = {}

    class Response:
        content = '{"ok":true}'
        usage_metadata = {}
        response_metadata = {}

    class FakeClient:
        def invoke(self, messages):
            return Response()

    def fake_client(**kwargs):
        captured.update(kwargs)
        return FakeClient()

    monkeypatch.setattr("langchain_openai.ChatOpenAI", fake_client)
    result, _ = NovelEngine()._llm_json_with_usage(
        "system", "user",
        {"id": "deepseek-reasoning-test", "provider": "deepseek", "api_key": "test-key", "base_url": "https://example.com/v1", "model": "deepseek", "reasoning_effort": "high"},
    )
    assert result == {"ok": True}
    assert "reasoning_effort" not in captured
    assert captured["extra_body"] == {"thinking": {"type": "enabled"}}


def test_unsupported_stream_falls_back_to_invoke(monkeypatch):
    progress = []

    class Response:
        content = '{"ok":true}'
        usage_metadata = {"input_tokens": 4, "output_tokens": 2, "total_tokens": 6}
        response_metadata = {}

    class FakeClient:
        def stream(self, messages):
            raise RuntimeError("stream is not supported")

        def invoke(self, messages):
            return Response()

    monkeypatch.setattr("langchain_openai.ChatOpenAI", lambda **kwargs: FakeClient())
    result, usage = NovelEngine()._llm_json_with_usage(
        "system",
        "user",
        {"id": "invoke-test", "api_key": "test-key", "base_url": "https://example.com/v1", "model": "fake"},
        0.1,
        on_progress=progress.append,
    )
    assert result == {"ok": True}
    assert usage["total_tokens"] == 6
    assert any("不支持流式输出" in message for message in progress)


def test_v2_batched_outline_replan_history_context_and_export(client):
    created = client.post("/api/works", json={"title": "V2 分批大纲", "premise": "主角必须修复一段被篡改的记忆。"})
    work_id = created.json()["id"]
    assert client.post(f"/api/works/{work_id}/generate/setup").status_code == 200

    initial = client.post(f"/api/works/{work_id}/generate/outline", json={
        "chapter_count": 2, "mode": "initial", "from_chapter": 1, "to_chapter": 2,
    })
    assert initial.status_code == 200, initial.text
    initial_data = initial.json()["data"]
    assert initial_data["target_chapter_count"] == 2
    assert initial_data["batches"] == [{"from_chapter": 1, "to_chapter": 2}]
    first_work = initial.json()["work"]
    assert len(first_work["chapter_plans"]) == 2
    assert all(len(plan["beats"]) == 5 for plan in first_work["chapter_plans"])
    assert all(plan["story_day"] is not None and plan["phase_key"] and plan["time_mode"] == "linear" for plan in first_work["chapter_plans"])
    original_chapter_one = first_work["chapter_plans"][0]["title"]

    extended = client.post(f"/api/works/{work_id}/generate/outline", json={
        "chapter_count": 20, "mode": "extend", "to_chapter": 20,
        "expected_outline_version": initial_data["outline_version"], "expected_fact_version": initial_data["fact_version"],
    })
    assert extended.status_code == 200, extended.text
    extended_data = extended.json()["data"]
    assert len(extended.json()["work"]["chapter_plans"]) == 20
    assert extended_data["batches"][-1] == {"from_chapter": 15, "to_chapter": 20}

    replanned = client.post(f"/api/works/{work_id}/generate/outline", json={
        "chapter_count": 20, "mode": "replan", "from_chapter": 3, "to_chapter": 20,
        "expected_outline_version": extended_data["outline_version"], "expected_fact_version": extended_data["fact_version"],
    })
    assert replanned.status_code == 200, replanned.text
    replanned_work = replanned.json()["work"]
    assert len(replanned_work["chapter_plans"]) == 20
    assert replanned_work["chapter_plans"][0]["title"] == original_chapter_one
    history = client.get(f"/api/works/{work_id}/chapter-plans/3/history")
    assert history.status_code == 200
    assert len(history.json()["items"]) >= 2
    context = client.get(f"/api/works/{work_id}/contexts/chapter?chapter_no=2")
    assert context.status_code == 200
    assert any(item["kind"] == "legacy_dynamic_card" for item in context.json()["context"]["excluded"])
    export = client.get(f"/api/works/{work_id}/outline/export")
    assert export.status_code == 200
    assert "causal_beats" in export.text and "state_changes" in export.text


def test_outline_normalizes_model_phase_label_to_configured_key(client, monkeypatch):
    created = client.post("/api/works", json={"title": "阶段键规范化", "premise": "主角必须找回一封被篡改的信。"})
    work_id = created.json()["id"]
    assert client.post(f"/api/works/{work_id}/generate/setup").status_code == 200

    original_generate_outline = engine.generate_outline

    def generate_with_narrative_label(*args, **kwargs):
        result = original_generate_outline(*args, **kwargs)
        for chapter in result["chapters"]:
            chapter["phase_key"] = "建立"
        return result

    monkeypatch.setattr(engine, "generate_outline", generate_with_narrative_label)
    response = client.post(f"/api/works/{work_id}/generate/outline", json={"chapter_count": 2})

    assert response.status_code == 200, response.text
    assert {item["phase_key"] for item in response.json()["work"]["chapter_plans"]} == {"default"}


def test_long_book_uses_global_target_but_only_generates_a_detail_window(client):
    created = client.post("/api/works", json={
        "title": "三百万字长篇", "premise": "末世幸存者从一支小队起步。",
        "estimated_words": 3_000_000, "average_chapter_words": 2500,
    })
    work_id = created.json()["id"]
    assert client.post(f"/api/works/{work_id}/generate/setup").status_code == 200

    response = client.post(f"/api/works/{work_id}/generate/outline", json={
        "mode": "initial", "total_target_chapters": 1200, "from_chapter": 1, "to_chapter": 12,
    })

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["data"]["total_target_chapters"] == 1200
    assert payload["data"]["batches"] == [{"from_chapter": 1, "to_chapter": 12}]
    work = payload["work"]
    assert len(work["chapter_plans"]) == 12
    assert max(volume["end_chapter"] for volume in work["story_volumes"]) == 1200
    first_stage = next(stage for stage in work["narrative_stages"] if stage["start_chapter"] == 1)
    assert first_stage["title"] == "开局建立"
    assert {plan["narrative_stage_id"] for plan in work["chapter_plans"]} == {first_stage["id"]}


def test_v2_rewrite_and_event_rollback_replay_current_facts(client, monkeypatch):
    created = client.post("/api/works", json={"title": "V2 事件重放", "premise": "主角在旧城寻找失踪亲人。"})
    work_id = created.json()["id"]
    assert client.post(f"/api/works/{work_id}/generate/setup").status_code == 200
    assert client.post(f"/api/works/{work_id}/generate/outline", json={"chapter_count": 2}).status_code == 200
    character_name = client.get(f"/api/works/{work_id}").json()["characters"][0]["name"]

    def fake_extract(_work, _chapter, profile=None):
        return {
            "characters": [{"character_name": character_name, "aliases": [], "changes": [{
                "field": "location", "old_value": None, "new_value": "旧档案室",
                "evidence": "主角走进旧档案室。", "confidence": 0.97,
            }]}],
            "timeline_events": [], "warnings": [],
        }

    monkeypatch.setattr(engine, "extract_state_changes", fake_extract)
    generated = client.post(f"/api/works/{work_id}/generate/chapter", json={"chapter_no": 1, "mode": "chapter"})
    assert generated.status_code == 200, generated.text
    extraction_items = []
    for _ in range(20):
        run_worker_once()
        extraction_items = client.get(f"/api/works/{work_id}/state-extractions").json()["items"]
        if extraction_items:
            break
    assert extraction_items
    extraction = extraction_items[0]
    accepted = client.post(
        f"/api/works/{work_id}/state-extractions/{extraction['id']}/review",
        json={"items": [{"id": extraction["characters"][0]["id"], "kind": "character", "action": "accept"}]},
    )
    assert accepted.status_code == 200, accepted.text
    assert client.get(f"/api/works/{work_id}").json()["chapter_plans"][1]["stale_reason"]
    before_second = client.get(f"/api/works/{work_id}/story-state?chapter_no=2&before_chapter=true").json()
    character_id = client.get(f"/api/works/{work_id}").json()["characters"][0]["id"]
    assert before_second["canonical_state"]["characters"][character_id]["location"] == "旧档案室"

    event_id = client.get(f"/api/works/{work_id}").json()["story_events"][-1]["id"]
    rolled = client.post(f"/api/works/{work_id}/story-events/{event_id}/rollback", json={"reason": "验证重放"})
    assert rolled.status_code == 200, rolled.text
    after_rollback = client.get(f"/api/works/{work_id}/story-state?chapter_no=2&before_chapter=true").json()
    assert character_id not in after_rollback["canonical_state"]["characters"]

    rewritten = client.patch(f"/api/works/{work_id}/chapters/1", json={"content": "主角没有进入旧档案室。"})
    assert rewritten.status_code == 200
    after_rewrite = client.get(f"/api/works/{work_id}/story-state?chapter_no=2&before_chapter=true").json()
    assert character_id not in after_rewrite["canonical_state"]["characters"]


def test_v2_long_term_facts_future_plans_and_goal_lifecycle(client):
    created = client.post("/api/works", json={"title": "V2 长期事实", "premise": "一份旧契约决定主角的选择。"})
    work_id = created.json()["id"]
    initial_version = client.get(f"/api/works/{work_id}").json()["fact_version"]

    fact = client.post(f"/api/works/{work_id}/long-term-facts", json={
        "entity_type": "world", "fact_key": "old_contract", "value": {"rule": "签字者不能泄露地点"}, "locked": True,
    })
    assert fact.status_code == 200, fact.text
    fact_work = fact.json()["work"]
    assert fact_work["fact_version"] > initial_version
    assert fact_work["long_term_facts"][0]["value"]["rule"] == "签字者不能泄露地点"

    goal = client.post(f"/api/works/{work_id}/goals", json={
        "title": "查清契约来源", "status": "planned", "priority": 3,
    })
    assert goal.status_code == 200, goal.text
    goal_id = goal.json()["goals"][0]["id"]
    updated_goal = client.patch(f"/api/works/{work_id}/goals/{goal_id}", json={
        "status": "active", "progress": {"percent": 25}, "chapter_no": 1, "evidence": "主角发现了签名页。",
    })
    assert updated_goal.status_code == 200, updated_goal.text
    state = client.get(f"/api/works/{work_id}/story-state?chapter_no=2&before_chapter=true").json()["canonical_state"]
    assert state["goals"][goal_id]["status"] == "active"
    assert state["goals"][goal_id]["progress"]["percent"] == 25

    future = client.post(f"/api/works/{work_id}/future-plans", json={
        "entity_type": "work", "plan_type": "reveal", "target_chapter": 8,
        "content": {"event": "公开旧契约"},
    })
    assert future.status_code == 200, future.text
    plan_id = future.json()["future_plans"][0]["id"]
    context = client.get(f"/api/works/{work_id}/contexts/chapter?chapter_no=1").json()["context"]
    assert context["long_term_facts"][0]["fact_key"] == "old_contract"
    assert any(item["kind"] == "future_plan" for item in context["excluded"])
    archived = client.patch(f"/api/works/{work_id}/future-plans/{plan_id}", json={"status": "archived"})
    assert archived.status_code == 200
    assert not archived.json()["future_plans"]


def test_saving_contract_draft_then_confirming_advances_step(client):
    created = client.post("/api/works", json={"title": "保存契约测试"})
    work_id = created.json()["id"]
    content = {"candidates": [{"title": "方向一", "target_experience": "明确回报"}, {"title": "方向二"}, {"title": "方向三"}]}
    saved = client.put(f"/api/works/{work_id}/planning-steps/contract/contract", json={"content": content})
    assert saved.status_code == 200
    assert saved.json()["status"] == "draft"
    confirmed = client.post(f"/api/works/{work_id}/planning-steps/contract/contract/confirm", json={"candidate_index": 0})
    assert confirmed.status_code == 200
    assert confirmed.json()["current_step"] == "setting"
