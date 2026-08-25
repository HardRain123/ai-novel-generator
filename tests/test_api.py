import json
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
from app.services import generation_jobs  # noqa: E402
from app.services.generation_jobs import run_worker_once  # noqa: E402
from app.services import model_profiles, novel_engine  # noqa: E402
from app.services import model_call_logs  # noqa: E402
from app.services.model_profiles import profile_for_task  # noqa: E402
from app.services.novel_engine import NovelEngine, _parse_json, codex_process_env, configured_prompt, engine  # noqa: E402
from app.services.planning_quality import evaluate_outline, language_risks, planning_checks, planning_consistency_checks, planning_coverage_checks, planning_field_rules  # noqa: E402
from app.services import trends  # noqa: E402
from app.services.generation_jobs import _planning_context  # noqa: E402
from app.services.planning_repository import _required_character_keys, confirmed_context, confirm_artifact, planning_context_for_step, upsert_artifact  # noqa: E402
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


def test_r1c_planning_rules_are_exposed_from_the_backend_source(client):
    response = client.get("/api/planning-rules")
    assert response.status_code == 200
    rules = response.json()
    assert rules["version"] == planning_field_rules()["version"]
    assert rules["default_rule"] == {"type": "text", "required": True, "hard": True, "min": 20, "max": 100}
    assert rules["steps"]["protagonist"]["biography"] == {"type": "text", "required": True, "hard": True, "min": 120, "max": 260}
    assert rules["steps"]["arc"]["synopsis"]["min"] == 120
    assert rules["steps"]["arc"]["synopsis"]["max"] == 300
    assert rules["steps"]["summary"]["summary"]["min"] == 180
    assert rules["steps"]["summary"]["summary"]["max"] == 350
    assert rules["steps"]["arc"]["title"]["min"] == 1
    assert rules["steps"]["arc"]["title"]["max"] == 40
    assert rules["steps"]["summary"]["ending"]["hard"] is False


def test_r1c_planning_hard_limits_accept_boundaries_and_block_outside_values():
    def has_issue(result, label):
        return any(label in issue for issue in result["blocking"])

    for length in (120, 260):
        assert not has_issue(planning_checks("protagonist", {"character": {"biography": "字" * length}}), "人物小传")
    for length in (119, 261):
        assert has_issue(planning_checks("protagonist", {"character": {"biography": "字" * length}}), "人物小传")

    for length in (180, 350):
        assert not has_issue(planning_checks("summary", {"story_bible": {"summary": "字" * length}}), "总梗概")
    for length in (179, 351):
        assert has_issue(planning_checks("summary", {"story_bible": {"summary": "字" * length}}), "总梗概")

    valid_setting = {field: "字" * 20 for field in ("core_hook", "core_conflict", "world", "stakes", "ending")}
    assert not has_issue(planning_checks("setting", {"story_bible": valid_setting}), "核心钩子")
    valid_setting["core_hook"] = "字" * 19
    assert has_issue(planning_checks("setting", {"story_bible": valid_setting}), "核心钩子")

    valid_arc = {
        "title": "一", "goal": "字" * 20, "opposition": "字" * 20,
        "turning_point": "字" * 20, "ending_state": "字" * 20, "synopsis": "字" * 120,
    }
    assert not has_issue(planning_checks("arc", {"arc": valid_arc}), "卷梗概")
    valid_arc["synopsis"] = "字" * 119
    assert has_issue(planning_checks("arc", {"arc": valid_arc}), "卷梗概")
    valid_arc["synopsis"] = "字" * 120
    valid_arc["title"] = ""
    assert has_issue(planning_checks("arc", {"arc": valid_arc}), "卷标题")
    valid_arc["title"] = "字" * 40
    assert not has_issue(planning_checks("arc", {"arc": valid_arc}), "卷标题")
    valid_arc["title"] = "字" * 41
    assert has_issue(planning_checks("arc", {"arc": valid_arc}), "卷标题")


def test_r1c_frontend_reads_field_ranges_and_marks_hard_limits_from_api():
    source = (Path(__file__).resolve().parents[1] / "web" / "app" / "page.tsx").read_text(encoding="utf-8")
    assert 'api<PlanningRules>("/planning-rules")' in source
    assert "planningFieldSpecs(step, content, rules)" in source
    assert 'field.hard ? "需填写" : "建议"' in source
    assert 'const contractFields: Array<[string, string, boolean?]>' in source
    assert "120, 260" not in source
    assert "180, 350" not in source


def test_planning_snapshots_can_restore_an_earlier_draft(client):
    created = client.post("/api/works", json={"title": "规划快照测试"})
    work_id = created.json()["id"]
    first = {"story_bible": {"summary": "第一版全书总梗概，从起点推进到结局。"}}
    second = {"story_bible": {"summary": "第二版全书总梗概，加入中点和最低谷。"}}

    assert client.put(f"/api/works/{work_id}/planning-steps/summary/default", json={"content": first}).status_code == 200
    assert client.put(f"/api/works/{work_id}/planning-steps/summary/default", json={"content": second}).status_code == 200
    snapshots = client.get(f"/api/works/{work_id}/planning-snapshots?step=summary&item_key=default")
    assert snapshots.status_code == 200
    items = snapshots.json()["items"]
    assert [item["version"] for item in items[:2]] == [2, 1]
    old_snapshot = next(item for item in items if item["version"] == 1)

    restored = client.post(f"/api/works/{work_id}/planning-snapshots/{old_snapshot['id']}/restore")
    assert restored.status_code == 200
    artifact = next(item for item in restored.json()["artifacts"] if item["step"] == "summary")
    assert artifact["content"] == first
    assert artifact["version"] == 3


def test_planning_session_exposes_full_book_coverage_for_the_wizard(client):
    created = client.post("/api/works", json={"title": "规划覆盖展示", "estimated_words": 100000, "target_chapter_count": 40})
    work_id = created.json()["id"]
    upsert_artifact(work_id, "arc", "arc:1", {"arc": {
        "title": "第一卷至终局",
        "start_chapter": 1,
        "end_chapter": 40,
        "synopsis": "从起点推进，经由中点和最低谷，进入最终清算与结局。",
    }})
    upsert_artifact(work_id, "summary", "default", {"story_bible": {
        "summary": "全书总梗概覆盖从起点、升级、中点、最低谷到最终清算和结局。",
    }})

    response = client.get(f"/api/works/{work_id}/planning-session")
    assert response.status_code == 200
    coverage = response.json()["coverage_checks"]["coverage"]
    assert coverage["planned_chapters"] == 40
    assert coverage["coverage_ratio"] == 1.0
    assert coverage["summary_scope"] == "full_book"


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


def test_p0e_demo_fallback_is_explicitly_marked(client):
    data = NovelEngine().generate_setup({"title": "演示模式状态"})

    assert data["generation_source"] == "fallback"
    assert data["repair_attempts"] == 0
    assert data["parse_status"] == "not_attempted"
    assert data["quality_status"] in {"passed", "failed"}


def test_p0e_configured_model_failure_fails_job_without_saving_fallback(client, monkeypatch):
    calls = []

    def invalid_model_response(*_args, **_kwargs):
        calls.append(True)
        return None

    monkeypatch.setattr(NovelEngine, "_llm_json", invalid_model_response)
    profile = client.post("/api/model-profiles", json={
        "name": f"P0-E 失败模型-{uuid4().hex}",
        "base_url": "https://example.com/v1",
        "model": "invalid-json-model",
        "api_key": "sk-p0e-test-123456",
    })
    assert profile.status_code == 200
    work = client.post("/api/works", json={
        "title": "真实模型失败不落 fallback",
        "model_profile_id": profile.json()["id"],
    })
    assert work.status_code == 200
    work_id = work.json()["id"]

    queued = client.post(f"/api/works/{work_id}/generation-jobs", json={"kind": "setup"})
    assert queued.status_code == 202
    assert run_worker_once() is True

    failed = client.get(f"/api/works/{work_id}/generation-jobs/{queued.json()['id']}").json()
    assert failed["status"] == "failed"
    assert "定向结构修复" in failed["error"]
    assert len(calls) == 2
    saved = client.get(f"/api/works/{work_id}").json()
    assert saved["story_bible"]["summary"] == ""
    assert saved["story_bible"]["generation_source"] == ""
    assert not saved.get("characters")
    assert client.patch(f"/api/model-profiles/{profile.json()['id']}", json={"enabled": False}).status_code == 200


def test_p0e_repair_calls_are_linked_in_model_call_observability(monkeypatch):
    requests = []
    finished = []

    class Response:
        usage_metadata = {}
        response_metadata = {}

        def __init__(self, content):
            self.content = content

    class FakeClient:
        def __init__(self):
            self.responses = [Response("not json"), Response('{"candidates": []}')]

        def invoke(self, _messages):
            return self.responses.pop(0)

    fake_client = FakeClient()

    def record_call(_profile, request):
        requests.append(request)
        return f"p0e-call-{len(requests)}"

    def finish_call(call_id, **kwargs):
        finished.append((call_id, kwargs.get("status")))

    monkeypatch.setattr(novel_engine, "start_model_call", record_call)
    monkeypatch.setattr(novel_engine, "finish_model_call", finish_call)
    monkeypatch.setattr("langchain_openai.ChatOpenAI", lambda **_kwargs: fake_client)

    data, _usage, source = NovelEngine().generate_planning_step(
        {"title": "修复调用关联"},
        "contract",
        "contract",
        {},
        profile={
            "id": "p0e-profile",
            "provider": "openai_compatible",
            "base_url": "https://example.com/v1",
            "model": "fake-model",
            "api_key": "sk-p0e-test-123456",
        },
    )

    assert data == {"candidates": []}
    assert source == "model"
    assert len(requests) == 2
    assert requests[1]["observability"]["repair_of_call_id"] == "p0e-call-1"
    assert requests[1]["observability"]["repair_attempt"] == 1
    assert finished == [("p0e-call-1", "success"), ("p0e-call-2", "success")]


def _character_batch_context():
    return {
        "cast_roster": [{
            "characters": [
                {"item_key": "character:1", "name": "顾遥", "role": "盟友", "story_function": "提供关键行动能力"},
                {"item_key": "character:2", "name": "陆衡", "role": "对手", "story_function": "制造持续压力"},
            ],
        }],
    }


def _character_batch_response(item_keys):
    return {
        "characters": [
            {"item_key": item_key, "character": {"name": f"模型人物-{item_key}"}}
            for item_key in item_keys
        ],
    }


def _configured_batch_profile():
    return {
        "id": "p0a-batch-profile",
        "provider": "openai_compatible",
        "base_url": "https://example.com/v1",
        "model": "batch-model",
        "api_key": "sk-p0a-batch-test",
    }


def test_p0a_character_batch_repairs_partial_response_without_fallback(monkeypatch):
    monkeypatch.setattr(novel_engine, "configured_prompt", lambda *_args: "planning")
    responses = [
        _character_batch_response(["character:1"]),
        _character_batch_response(["character:1", "character:2"]),
    ]
    calls = []

    def fake_batch_call(_system, user, *_args, **_kwargs):
        calls.append(user)
        return responses.pop(0), {}

    monkeypatch.setattr(NovelEngine, "_llm_json_with_usage", fake_batch_call)
    result, _usage, source = NovelEngine().generate_character_batch(
        {"title": "批量人物修复"},
        ["character:1", "character:2"],
        _character_batch_context(),
        profile=_configured_batch_profile(),
    )

    assert len(calls) == 2
    assert set(result) == {"character:1", "character:2"}
    assert {item["name"] for item in result.values()} == {"顾遥", "陆衡"}
    assert source == "model"


@pytest.mark.parametrize(
    "invalid_result",
    [
        {"characters": [
            {"item_key": "character:1", "character": {"name": "重复一"}},
            {"item_key": "character:1", "character": {"name": "重复二"}},
        ]},
        {"characters": [
            {"item_key": "character:1", "character": {"name": "顾遥"}},
            {"item_key": "character:unknown", "character": {"name": "未知"}},
        ]},
        {"characters": [
            {"item_key": "character:1", "character": "不是对象"},
            {"item_key": "character:2", "character": {"name": "陆衡"}},
        ]},
    ],
)
def test_p0a_character_batch_repairs_invalid_item_keys_or_character_shape(monkeypatch, invalid_result):
    monkeypatch.setattr(novel_engine, "configured_prompt", lambda *_args: "planning")
    responses = [invalid_result, _character_batch_response(["character:1", "character:2"])]
    call_count = 0

    def fake_batch_call(*_args, **_kwargs):
        nonlocal call_count
        call_count += 1
        return responses.pop(0), {}

    monkeypatch.setattr(NovelEngine, "_llm_json_with_usage", fake_batch_call)
    result, _usage, source = NovelEngine().generate_character_batch(
        {"title": "批量人物键校验"},
        ["character:1", "character:2"],
        _character_batch_context(),
        profile=_configured_batch_profile(),
    )

    assert call_count == 2
    assert set(result) == {"character:1", "character:2"}
    assert source == "model"


def test_p0a_character_batch_repair_failure_raises_without_fallback(monkeypatch):
    monkeypatch.setattr(novel_engine, "configured_prompt", lambda *_args: "planning")
    calls = []

    def fake_batch_call(*_args, **_kwargs):
        calls.append(True)
        return _character_batch_response(["character:1"]), {}

    engine_instance = NovelEngine()
    fallback_calls = []
    monkeypatch.setattr(NovelEngine, "_llm_json_with_usage", fake_batch_call)
    monkeypatch.setattr(
        engine_instance,
        "_planning_fallback",
        lambda *_args, **_kwargs: fallback_calls.append(True),
    )

    with pytest.raises(ValueError, match="定向结构修复"):
        engine_instance.generate_character_batch(
            {"title": "批量人物修复失败"},
            ["character:1", "character:2"],
            _character_batch_context(),
            profile=_configured_batch_profile(),
        )

    assert len(calls) == 2
    assert fallback_calls == []


def test_p0a_configured_batch_failure_does_not_save_partial_drafts(client, monkeypatch):
    monkeypatch.setattr(novel_engine, "configured_prompt", lambda *_args: "planning")
    profile = client.post("/api/model-profiles", json={
        "name": f"P0-A 批量失败-{uuid4().hex}",
        "base_url": "https://example.com/v1",
        "model": "batch-invalid-model",
        "api_key": "sk-p0a-batch-test-123456",
    })
    assert profile.status_code == 200
    work = client.post("/api/works", json={"title": "批量失败不落库"})
    work_id = work.json()["id"]
    monkeypatch.setattr(generation_jobs, "_planning_context", lambda *_args, **_kwargs: _character_batch_context())

    def invalid_batch_call(*_args, **_kwargs):
        return _character_batch_response(["character:1"]), {}

    monkeypatch.setattr(NovelEngine, "_llm_json_with_usage", invalid_batch_call)
    queued = client.post(
        f"/api/works/{work_id}/generation-jobs",
        json={
            "kind": "planning_character_batch",
            "payload": {"item_keys": ["character:1", "character:2"]},
            "model_profile_id": profile.json()["id"],
        },
    )
    assert queued.status_code == 202, queued.text
    assert run_worker_once() is True

    failed = client.get(f"/api/works/{work_id}/generation-jobs/{queued.json()['id']}").json()
    assert failed["status"] == "failed"
    assert "定向结构修复" in failed["error"]
    session = client.get(f"/api/works/{work_id}/planning-session").json()
    assert not [item for item in session["artifacts"] if item["step"] == "character"]
    assert client.patch(f"/api/model-profiles/{profile.json()['id']}", json={"enabled": False}).status_code == 200


def test_character_batch_quality_is_written_to_the_final_model_call(client, monkeypatch):
    profile = client.post("/api/model-profiles", json={
        "name": f"R1-A 批量质检-{uuid4().hex}",
        "base_url": "https://example.com/v1",
        "model": "r1a-batch-model",
        "api_key": "sk-r1a-batch-test-123456",
    })
    assert profile.status_code == 200, profile.text
    work = client.post("/api/works", json={"title": "批量人物调用状态"})
    work_id = work.json()["id"]
    monkeypatch.setattr(generation_jobs, "_planning_context", lambda *_args, **_kwargs: _character_batch_context())
    monkeypatch.setattr(
        generation_jobs,
        "character_batch_checks",
        lambda drafts, _context: {
            item_key: {"blocking": [], "warnings": [], "ok": True}
            for item_key in drafts
        },
    )

    def successful_batch_call(self, _system, user, profile, *_args, **_kwargs):
        item_keys = [item["item_key"] for item in json.loads(user)["selected_roster_characters"]]
        response = _character_batch_response(item_keys)
        call_id = model_call_logs.start_model_call(
            profile,
            {"transport": "openai", "model": profile.get("model"), "stream": False},
        )
        self._last_model_call_id = call_id
        model_call_logs.finish_model_call(
            call_id,
            status="success",
            response_text=json.dumps(response, ensure_ascii=False),
            response=response,
            parse_status="success",
        )
        return response, {}

    monkeypatch.setattr(NovelEngine, "_llm_json_with_usage", successful_batch_call)
    queued = client.post(
        f"/api/works/{work_id}/generation-jobs",
        json={
            "kind": "planning_character_batch",
            "payload": {"item_keys": ["character:1", "character:2"]},
            "model_profile_id": profile.json()["id"],
        },
    )
    assert queued.status_code == 202, queued.text
    assert run_worker_once() is True
    job_id = queued.json()["id"]
    job = client.get(f"/api/works/{work_id}/generation-jobs/{job_id}").json()
    assert job["status"] == "completed", job
    calls = client.get(f"/api/model-call-logs?work_id={work_id}&limit=10").json()["items"]
    item = next(call for call in calls if call["generation_job_id"] == job_id)
    assert item["parse_status"] == "success"
    assert item["quality_status"] == "passed"
    assert item["adoption_status"] == "adopted"
    assert client.patch(f"/api/model-profiles/{profile.json()['id']}", json={"enabled": False}).status_code == 200


def test_p0a_character_batch_demo_fallback_remains_complete(monkeypatch):
    monkeypatch.setattr(novel_engine, "configured_prompt", lambda *_args: "planning")
    monkeypatch.setattr(NovelEngine, "_llm_json_with_usage", lambda *_args, **_kwargs: (None, {}))

    result, _usage, source = NovelEngine().generate_character_batch(
        {"title": "演示批量人物"},
        ["character:1", "character:2"],
        _character_batch_context(),
        profile=None,
    )

    assert set(result) == {"character:1", "character:2"}
    assert source == "fallback"


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
    assert client.patch(f"/api/model-profiles/{profile.json()['id']}", json={"enabled": False}).status_code == 200


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


def test_planning_summary_promotes_single_candidate_wrapper(client, monkeypatch):
    generated = {
        "candidate_count": 1,
        "candidates": [{
            "story_bible": {
                "summary": "一段足够完整的故事梗概。" * 20,
                "theme": "人在压力下仍要为自己的规则负责。",
                "style_rules": "短句推进，行动体现选择。",
            }
        }],
    }
    monkeypatch.setattr(
        NovelEngine,
        "_llm_json_with_usage",
        lambda *_args, **_kwargs: (generated, {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3}),
    )

    data, usage, source = NovelEngine().generate_planning_step(
        {"title": "候选包装测试"}, "summary", "default", {},
    )

    assert data == generated["candidates"][0]
    assert data["story_bible"]["summary"]
    assert usage["total_tokens"] == 3
    assert source == "model"


def test_planning_context_for_step_selects_contract_and_compacts_summary(client):
    created = client.post("/api/works", json={"title": "上下文净化测试"})
    work_id = created.json()["id"]

    def save_and_confirm(step, item_key, content):
        upsert_artifact(work_id, step, item_key, content)
        confirm_artifact(work_id, step, item_key)

    contract_a = {"title": "契约 A", "body": "A 正文" * 220, "style_rules": "具体行动推动情节并保持明确回报节奏和人物选择"}
    contract_b = {
        "title": "契约 B",
        "body": "B 正文" * 220,
        "style_rules": "具体行动推动情节并保持明确回报节奏和人物选择",
        "must_have_elements": ["B 的核心回报"],
    }
    contract_c = {"title": "契约 C", "body": "C 正文" * 220, "style_rules": "具体行动推动情节并保持明确回报节奏和人物选择"}
    save_and_confirm("contract", "contract", {"candidates": [contract_a, contract_b, contract_c], "selected": contract_b})
    save_and_confirm("setting", "default", {"story_bible": {
        "core_hook": "核心设定起点：旧城封锁迫使主角主动介入。",
        "core_conflict": "主角要公开证据，对手要保住既有秩序，双方不能同时达成目标。",
        "world": "证据只能通过有限的记忆设备读取，每次读取都会消耗一次机会。",
        "stakes": "失败会失去证据和同伴，成功也会暴露主角过去的责任。",
        "ending": "主角在最终冲突中公开证据并承担关系破裂的代价。",
        "irrelevant": "不应进入摘要上下文",
    }})
    save_and_confirm("protagonist", "default", {"character": {
        "name": "沈峙", "role": "主角", "goal": "在封锁结束前公开证据并保护仍愿意作证的人",
        "conflict": "对手持续销毁证据并利用旧案责任逼迫他保持沉默",
        "motivation": "弥补当年因沉默造成的伤害并承担公开真相的代价",
        "flaw": "习惯独自确认全部信息后才行动，常常错过保护同伴的时机",
        "character_arc": "从谨慎旁观转向承担公开真相和关系破裂的代价",
        "appearance": "身形清瘦但肩背挺直，眉眼有一道旧伤，常穿磨白的深色工装外套，左手指节留着烧伤痕迹。",
        "personality": "他习惯先记录证据再做判断，遇到威胁时仍会把同伴安全放在选择前面。",
        "voice": "他说话短而具体，先说明行动和代价，再要求对方回答能否承担后果。",
        "biography": "沈峙曾因一次沉默让关键证人失去保护，此后他把所有选择都拆成可核验的证据链。封锁旧城后，他发现当年的责任并未结束，反而被新的权力关系重新利用。为了阻止证据被销毁，他必须一边说服不再信任他的同伴，一边承认自己也曾从沉默中获利，最终选择公开真相并接受生活彻底改变。",
        "facets": {},
    }})
    save_and_confirm("cast_roster", "default", {"characters": [
        {"item_key": "character:1", "name": "顾遥", "role": "盟友", "story_function": "提供关键行动能力", "relationship_to_protagonist": "从互相利用到共同承担风险"},
        {"item_key": "character:2", "name": "陆衡", "role": "对手", "story_function": "持续制造外部压力", "relationship_to_protagonist": "试图让主角放弃公开证据"},
    ]})
    save_and_confirm("character", "character:1", {"character": {
        "name": "顾遥", "role": "盟友", "story_function": "提供关键行动能力", "biography": "顾遥曾因一次错误判断失去家人，因此决定把证据送到真正能使用它的人手中。她多年在封锁区边缘替人运送物资，熟悉每条检查线的漏洞，也清楚任何一次冒险都会把无辜者拖进旧案。她最初只相信自己准备的退路，直到主角愿意公开承认责任，她才决定把证据和自己的名字一起交出去。",
            "dramatic_core": {"goal": "在证据被销毁前把它送到公开审查的渠道并保护证人", "motivation": "弥补过去失误造成的伤害并让家人得到迟来的解释", "flaw": "过度依赖自己的退路，常常在同伴需要信任时先替他们做决定", "conflict": "不信任主角的犹豫，却又必须借助他的公开身份突破封锁"},
            "character_arc": "从只相信个人退路，转向愿意把选择和风险交给同伴共同承担",
            "appearance": "身形利落，短发贴着额角，眉尾有细疤，穿旧防水外套和硬底靴，右手腕留着一圈灼痕作为辨识标记。",
        "personality": "她先观察退路和代价，再决定是否把别人纳入自己的计划，真正承诺后又极少后退。",
        "voice": "她说话直接，习惯先报地点、时间和风险，拒绝用含糊的安慰替代行动安排。",
        "facets": {},
        "relationships": "她与主角先互相利用，后来因为共同承担公开证据的风险而建立了可验证的信任。",
    }})
    save_and_confirm("arc", "arc:1", {"arc": {
        "title": "第一卷", "sequence": 1, "goal": "找到并保护原始证据，同时确认谁有能力公开它", "opposition": "对手利用封锁和舆论控制证人，让每条安全路线都承担新的暴露风险", "turning_point": "主角发现自己也参与了旧案的沉默，并且当年的选择仍在伤害盟友",
        "ending_state": "证据暂时保住，但主角身份暴露，盟友要求他公开承担旧案责任", "synopsis": "主角从封锁起点出发，联合盟友突破检查线并追查原始证据的去向。中段他发现责任链也指向自己，原本的安全方案因此失效。卷末对手准备销毁证据时，主角选择公开自己的旧案责任，换取盟友继续把证据送入审查程序。这个决定同时改变了盟友对他的判断，也把下一卷的公开行动变成无法回避的现实。",
        "payoffs": ["不应进入摘要上下文"],
    }})
    save_and_confirm("arc", "arc:2", {"arc": {
        "title": "第二卷", "sequence": 2, "goal": "完成最终公开并让证人获得可持续的保护，同时建立新的审查渠道", "opposition": "对手用同伴安全和伪造证据逼迫主角撤回公开承诺", "turning_point": "对手用同伴安全逼迫主角撤回证据，主角却发现唯一的突破口要求他承认更大的责任",
        "ending_state": "真相公开，主角失去原有身份，但证人和盟友获得了继续行动的空间", "synopsis": "主角在最低谷重新组织证据链，先承认公开行动带来的牺牲，再联合证人拆穿伪造材料。最终他以不可撤销的选择兑现创作契约，虽然失去原有身份，却让真相进入无法被单方撤回的审查流程，并让所有曾经依赖沉默获利的人面对新的责任和后果。证人因此获得持续保护，盟友也有机会在公开记录中恢复自己的选择权。",
    }})

    full_context = confirmed_context(work_id)
    summary_context = planning_context_for_step(work_id, "summary")
    full_chars = len(json.dumps(full_context, ensure_ascii=False))
    summary_chars = len(json.dumps(summary_context, ensure_ascii=False))
    assert summary_chars <= full_chars * 0.6

    summary_text = json.dumps(summary_context, ensure_ascii=False)
    assert "契约 B" in summary_text and "B 正文" in summary_text
    assert "契约 A" not in summary_text and "契约 C" not in summary_text
    assert "appearance" not in summary_text
    assert "voice" not in summary_text
    assert "facets" not in summary_text
    assert "核心设定起点" in summary_text
    assert "最终冲突中公开证据" in summary_text
    assert "从互相利用到共同承担风险" in summary_text
    assert "完成最终公开" in summary_text


def test_model_call_records_planning_context_block_char_counts(monkeypatch):
    captured = {}

    class Response:
        content = '{"ok": true}'
        usage_metadata = {}
        response_metadata = {}

    class FakeClient:
        def invoke(self, _messages):
            return Response()

    def record_call(_profile, request):
        captured.update(request)
        return "planning-call"

    monkeypatch.setattr(novel_engine, "start_model_call", record_call)
    monkeypatch.setattr(novel_engine, "finish_model_call", lambda *_args, **_kwargs: None)
    fake_client = FakeClient()
    monkeypatch.setattr("langchain_openai.ChatOpenAI", lambda **_kwargs: fake_client)
    metadata = {"context_char_counts": {"contract": 28, "setting": 41}, "context_chars_total": 69}

    result, _usage = NovelEngine()._llm_json_with_usage(
        "system", "user",
        {"id": "planning-observability", "api_key": "test-key", "base_url": "https://example.com/v1", "model": "fake"},
        0.1,
        stream=False,
        request_metadata=metadata,
    )

    assert result == {"ok": True}
    assert captured["observability"] == metadata


def test_model_call_page_projection_includes_task_context_and_repair_chain(client):
    created = client.post("/api/works", json={"title": "调用观测测试"})
    work_id = created.json()["id"]
    queued = client.post(
        f"/api/works/{work_id}/generation-jobs",
        json={"kind": "planning_step", "payload": {"step": "summary", "item_key": "default"}},
    )
    job_id = queued.json()["id"]
    profile = {"provider": "openai_compatible", "model": "actual-model", "base_url": "https://actual.example/v1"}
    first = model_call_logs.start_model_call(
        profile,
        {"transport": "openai", "model": "actual-model", "temperature": 0.4, "reasoning_effort": "high", "timeout_seconds": 33, "observability": {"context_char_counts": {"contract": 40, "setting": 60}, "context_chars_total": 100}},
        call_kind="planning_step", work_id=work_id, generation_job_id=job_id,
    )
    model_call_logs.finish_model_call(
        first,
        status="success",
        response_text='{"ok": true}',
        response={"ok": True},
        parse_status="failed",
        quality_status="not_checked",
        adoption_status="not_adopted",
    )
    repair = model_call_logs.start_model_call(
        profile,
        {"transport": "openai", "model": "actual-model", "temperature": 0.2, "reasoning_effort": "high", "timeout_seconds": 33, "observability": {"repair_of_call_id": first}},
        call_kind="planning_step", work_id=work_id, generation_job_id=job_id,
    )
    model_call_logs.finish_model_call(
        repair,
        status="success",
        response_text='{"ok": true}',
        response={"ok": True},
        parse_status="success",
        quality_status="passed",
        adoption_status="adopted",
    )
    with transaction() as conn:
        conn.execute(
            "UPDATE generation_jobs SET status='completed', output_json=?, completed_at=?, updated_at=? WHERE id=?",
            (json.dumps({"parse_status": "repaired", "quality_status": "passed"}, ensure_ascii=False), now_iso(), now_iso(), job_id),
        )

    listed = client.get(f"/api/model-call-logs?work_id={work_id}&limit=10")
    assert listed.status_code == 200
    first_item = next(entry for entry in listed.json()["items"] if entry["id"] == first)
    item = next(entry for entry in listed.json()["items"] if entry["id"] == repair)
    assert item["work_title"] == "调用观测测试"
    assert item["task_name"] == "规划：summary"
    assert item["planning_step"] == "summary"
    assert item["item_key"] == "default"
    assert item["generation_task_url"].endswith(f"/works/{work_id}/generation-jobs/{job_id}")
    assert item["transmission_status"] == "delivered"
    assert item["parse_status"] == "success"
    assert item["quality_status"] == "passed"
    assert item["adoption_status"] == "adopted"
    assert item["effective_parameters"]["timeout_seconds"] == 33
    assert first_item["parse_status"] == "failed"
    assert first_item["adoption_status"] == "not_adopted"
    assert first_item["failure_category"] == "structure_error"

    detail = client.get(f"/api/model-call-logs/{repair}")
    assert detail.status_code == 200
    detail_data = detail.json()
    assert [entry["id"] for entry in detail_data["call_chain"]] == [first, repair]
    assert detail_data["context_char_shares"] == {}
    expected_request_chars = len(json.dumps(
        {"transport": "openai", "model": "actual-model", "temperature": 0.2, "reasoning_effort": "high", "timeout_seconds": 33, "observability": {"repair_of_call_id": first}},
        ensure_ascii=False,
    ))
    expected_response_chars = len('{"ok": true}')
    assert isinstance(item["request_chars"], int)
    assert isinstance(item["response_chars"], int)
    assert (item["request_chars"], item["response_chars"]) == (expected_request_chars, expected_response_chars)
    assert (detail_data["request_chars"], detail_data["response_chars"]) == (expected_request_chars, expected_response_chars)
    assert detail_data["response_text"] == '{"ok": true}'
    assert all(isinstance(entry["request_chars"], int) and isinstance(entry["response_chars"], int) for entry in detail_data["call_chain"])
    assert all((entry["request_chars"], entry["response_chars"]) == (expected_request_chars if entry["id"] == repair else len(json.dumps(
        {"transport": "openai", "model": "actual-model", "temperature": 0.4, "reasoning_effort": "high", "timeout_seconds": 33, "observability": {"context_char_counts": {"contract": 40, "setting": 60}, "context_chars_total": 100}},
        ensure_ascii=False,
    )), expected_response_chars) for entry in detail_data["call_chain"])


def test_invalid_model_json_repair_persists_call_level_states(client, monkeypatch):
    profile = client.post("/api/model-profiles", json={
        "name": f"R1-A 修复链-{uuid4().hex}",
        "base_url": "https://example.com/v1",
        "model": "r1a-repair-model",
        "api_key": "sk-r1a-repair-test-123456",
    })
    assert profile.status_code == 200, profile.text
    work = client.post("/api/works", json={"title": "非法 JSON 修复链"})
    work_id = work.json()["id"]
    summary = "主角在旧档案中发现被篡改的证据，必须在追查真相与保护证人之间作出选择。"
    while len(summary) < 180:
        summary += "每一步行动都会改变人物关系、资源代价和最终清算，新的事实也会逼迫主角重新承担选择后果。"

    class Response:
        usage_metadata = {}
        response_metadata = {}

        def __init__(self, content):
            self.content = content

    class FakeClient:
        def __init__(self):
            self.responses = [Response("not json"), Response(json.dumps({"story_bible": {"summary": summary}}, ensure_ascii=False))]

        def stream(self, _messages):
            raise RuntimeError("stream is not supported")

        def invoke(self, _messages):
            return self.responses.pop(0)

    fake_client = FakeClient()
    monkeypatch.setattr("langchain_openai.ChatOpenAI", lambda **_kwargs: fake_client)
    queued = client.post(
        f"/api/works/{work_id}/generation-jobs",
        json={
            "kind": "planning_step",
            "payload": {"step": "summary", "item_key": "default"},
            "model_profile_id": profile.json()["id"],
        },
    )
    assert queued.status_code == 202, queued.text
    assert run_worker_once() is True
    job_id = queued.json()["id"]
    job = client.get(f"/api/works/{work_id}/generation-jobs/{job_id}").json()
    assert job["status"] == "completed", job.get("error")
    calls = client.get(f"/api/model-call-logs?work_id={work_id}&limit=10").json()["items"]
    chain = sorted((call for call in calls if call["generation_job_id"] == job_id), key=lambda call: call["created_at"])
    assert len(chain) == 2
    assert chain[0]["parse_status"] == "invalid"
    assert chain[0]["adoption_status"] == "not_adopted"
    assert chain[0]["failure_category"] == "structure_error"
    assert chain[1]["repair_of_call_id"] == chain[0]["id"]
    assert chain[1]["transmission_status"] == "delivered"
    assert chain[1]["parse_status"] == "success"
    assert chain[1]["quality_status"] == "passed"
    assert chain[1]["adoption_status"] == "adopted"
    assert all(call["effective_parameters"]["transport"] == "openai" for call in chain)
    assert client.patch(f"/api/model-profiles/{profile.json()['id']}", json={"enabled": False}).status_code == 200


def test_model_call_log_character_counts_default_to_zero_for_empty_payloads(client):
    call_id = model_call_logs.start_model_call({"provider": "test", "model": "empty-model"}, {"request": "will be cleared"})
    with transaction() as conn:
        conn.execute("UPDATE model_call_logs SET request_json='' WHERE id=?", (call_id,))
    model_call_logs.finish_model_call(call_id, status="success", response_text="")

    listed = client.get("/api/model-call-logs?limit=10")
    item = next(entry for entry in listed.json()["items"] if entry["id"] == call_id)
    detail = client.get(f"/api/model-call-logs/{call_id}").json()
    assert item["request_chars"] == item["response_chars"] == 0
    assert detail["request_chars"] == detail["response_chars"] == 0
    assert detail["response_text"] == ""


def test_model_call_stats_separate_failure_categories(client):
    created = client.post("/api/works", json={"title": "调用失败分类"})
    work_id = created.json()["id"]
    profile = {"provider": "test", "model": "test-model", "base_url": "https://example.test"}
    for status, error, parse_status, quality_status in (
        ("timeout", "provider timeout", "not_recorded", "not_recorded"),
        ("canceled", "用户取消", "not_recorded", "not_recorded"),
        ("failed", "JSON parse error", "failed", "not_checked"),
        ("failed", "quality gate failed", "success", "failed"),
    ):
        call_id = model_call_logs.start_model_call(profile, {"model": "test-model"}, work_id=work_id)
        model_call_logs.finish_model_call(
            call_id,
            status=status,
            error=error,
            parse_status=parse_status,
            quality_status=quality_status,
            adoption_status="not_adopted",
        )

    stats = client.get(f"/api/model-call-logs/stats?work_id={work_id}")
    assert stats.status_code == 200
    assert stats.json()["failure_categories"] == {
        "timeout": 1,
        "structure_error": 1,
        "quality_failure": 1,
        "user_canceled": 1,
        "transport_failure": 0,
    }


def test_model_call_quality_failure_keeps_transport_and_parse_success(client):
    created = client.post("/api/works", json={"title": "质检失败不采用"})
    work_id = created.json()["id"]
    call_id = model_call_logs.start_model_call(
        {"provider": "test", "model": "quality-model", "base_url": "https://example.test"},
        {"transport": "openai", "model": "quality-model", "stream": False},
        work_id=work_id,
    )
    model_call_logs.finish_model_call(
        call_id,
        status="success",
        response_text='{"ok": true}',
        response={"ok": True},
        parse_status="success",
        quality_status="failed",
        adoption_status="not_adopted",
    )

    item = next(entry for entry in client.get(f"/api/model-call-logs?work_id={work_id}").json()["items"] if entry["id"] == call_id)
    assert item["transmission_status"] == "delivered"
    assert item["parse_status"] == "success"
    assert item["quality_status"] == "failed"
    assert item["adoption_status"] == "not_adopted"
    assert item["failure_category"] == "quality_failure"


def test_cast_roster_prompt_and_quality_gate_exclude_protagonist(client, monkeypatch):
    captured = {}

    def fake_llm(_self, _system, user, *_args, **_kwargs):
        captured.update(json.loads(user))
        return {"characters": []}, {},

    monkeypatch.setattr(NovelEngine, "_llm_json_with_usage", fake_llm)
    context = {"protagonist": [{"character": {"name": "沈峙", "role": "主角"}}]}
    data, _usage, source = NovelEngine().generate_planning_step(
        {"title": "阵容规则测试"}, "cast_roster", "default", context,
    )

    assert data == {"characters": []}
    assert source == "model"
    assert "配角、盟友和对手" in captured["cast_roster_rules"]
    assert "不得包含主角" in captured["schema"]["description"]
    checks = planning_checks(
        "cast_roster",
        {"characters": [{"item_key": "character:1", "name": "沈-峙", "role": "盟友"}]},
        context,
    )
    assert any("不能包含主角" in issue for issue in checks["blocking"])


def test_required_character_keys_exclude_protagonist_roster_entry():
    artifacts = [
        {"step": "protagonist", "status": "confirmed", "content": {"character": {"name": "沈峙"}}},
        {"step": "cast_roster", "status": "confirmed", "content": {"characters": [
            {"item_key": "character:1", "name": "沈-峙"},
            {"item_key": "character:2", "name": "顾遥"},
        ]}},
    ]

    assert _required_character_keys(artifacts) == ["character:2"]


def test_cast_roster_with_protagonist_name_cannot_be_confirmed(client):
    created = client.post("/api/works", json={"title": "阵容确认阻断"})
    work_id = created.json()["id"]
    upsert_artifact(work_id, "protagonist", "default", {"character": {
        "name": "沈峙", "role": "主角",
        "biography": "沈峙曾因一次沉默让关键证人失去保护，此后他把每个选择都拆成可以核验的证据链，并决心承担公开真相的代价。封锁旧城后，他发现当年的责任被新的权力关系重新利用，只能一边说服不再信任他的同伴，一边承认自己也曾从沉默中获利，最终选择公开真相并接受生活彻底改变。",
        "goal": "在封锁结束前公开证据并保护愿意作证的同伴",
        "conflict": "对手持续销毁证据并利用旧案责任逼迫他保持沉默",
        "motivation": "弥补当年因沉默造成的伤害并承担公开真相的代价",
        "flaw": "习惯独自确认全部信息后才行动，常常错过保护同伴的时机",
        "character_arc": "从谨慎旁观转向承担公开真相和关系破裂的代价",
        "appearance": "身形清瘦但肩背挺直，眉眼有一道旧伤，常穿磨白的深色工装外套，左手指节留着烧伤痕迹。",
        "personality": "他习惯先记录证据再做判断，遇到威胁时仍会把同伴安全放在选择前面。",
        "voice": "他说话短而具体，先说明行动和代价，再要求对方回答能否承担后果。",
    }})
    confirm_artifact(work_id, "protagonist", "default")
    roster = {"characters": [{
        "item_key": "character:1", "name": "沈峙", "role": "盟友",
        "story_function": "制造行动支点", "relationship_to_protagonist": "与主角互相利用",
    }]}

    saved = client.put(f"/api/works/{work_id}/planning-steps/cast_roster/default", json={"content": roster})
    assert saved.status_code == 200
    blocked = client.post(f"/api/works/{work_id}/planning-steps/cast_roster/default/confirm", json={})
    assert blocked.status_code == 409
    assert "不能包含主角" in blocked.text
    artifact = next(item for item in client.get(f"/api/works/{work_id}/planning-session").json()["artifacts"] if item["step"] == "cast_roster")
    assert artifact["status"] == "draft"


def test_finalize_rejects_legacy_duplicate_protagonist_instead_of_silent_dedupe(client):
    created = client.post("/api/works", json={"title": "旧重复主角"})
    work_id = created.json()["id"]

    def save_legacy_confirmed(step, item_key, content):
        upsert_artifact(work_id, step, item_key, content)
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                """
                UPDATE planning_artifacts SET status='confirmed', confirmed_at='legacy'
                WHERE session_id=(SELECT id FROM planning_sessions WHERE work_id=?)
                  AND step=? AND item_key=?
                """,
                (work_id, step, item_key),
            )
            conn.commit()

    save_legacy_confirmed("contract", "contract", {"selected": {"title": "明确方向", "style_rules": "具体行动推动情节"}})
    save_legacy_confirmed("setting", "default", {"story_bible": {
        "ending": "主角公开真相并承担代价。",
        "rules": {"allowed": ["夜间传送"], "forbidden": ["夜间传送"]},
    }})
    save_legacy_confirmed("protagonist", "default", {"character": {"name": "沈峙", "role": "主角"}})
    save_legacy_confirmed("cast_roster", "default", {"characters": [{
        "item_key": "character:1", "name": "顾遥", "role": "盟友",
        "story_function": "提供行动支点", "relationship_to_protagonist": "与主角建立信任",
    }]})
    save_legacy_confirmed("character", "character:1", {"character": {"name": "顾遥", "role": "盟友"}})
    save_legacy_confirmed("character", "character:2", {"character": {"name": "沈峙", "role": "主角副本"}})
    save_legacy_confirmed("arc", "arc:1", {"arc": {"title": "第一卷", "goal": "完成阶段目标"}})
    save_legacy_confirmed("summary", "default", {"story_bible": {"summary": "一份完整的全书总梗概。"}})

    finalized = client.post(f"/api/works/{work_id}/planning-session/finalize")
    assert finalized.status_code == 409
    assert "重复主角" in finalized.text
    assert "规则直接冲突" in finalized.text


def test_finalize_merges_summary_and_selected_contract_fields_stably(client):
    created = client.post("/api/works", json={"title": "最终合并语义测试"})
    work_id = created.json()["id"]

    def save_legacy_confirmed(step, item_key, content):
        upsert_artifact(work_id, step, item_key, content)
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                """
                UPDATE planning_artifacts SET status='confirmed', confirmed_at='legacy'
                WHERE session_id=(SELECT id FROM planning_sessions WHERE work_id=?)
                  AND step=? AND item_key=?
                """,
                (work_id, step, item_key),
            )
            conn.commit()

    save_legacy_confirmed("contract", "contract", {
        "selected": {
            "title_interpretation": "选中契约解释书名并兑现承诺",
            "reader_promise": "选中契约承诺主动行动和清晰回报",
            "style_rules": "被选契约文风，不应覆盖总结文风",
            "must_have_elements": ["契约元素", "共享元素"],
            "avoid_drift": ["契约禁区", "共享边界"],
        },
        "candidates": [{"title": "被选方向"}, {"title": "被放弃方向", "must_have_elements": ["被放弃元素"]}],
    })
    save_legacy_confirmed("setting", "default", {"story_bible": {
        "world": "世界规则保留有限资源和明确代价。",
        "ending": "主角公开真相并承担关系破裂的代价。",
        "must_have_elements": ["设定元素", "共享元素"],
        "avoid_drift": ["设定禁区", "共享边界"],
    }})
    save_legacy_confirmed("protagonist", "default", {"character": {"name": "沈峙", "role": "主角"}})
    save_legacy_confirmed("cast_roster", "default", {"characters": [{
        "item_key": "character:1", "name": "顾遥", "role": "盟友",
    }]})
    save_legacy_confirmed("character", "character:1", {"character": {"name": "顾遥", "role": "盟友"}})
    save_legacy_confirmed("arc", "arc:1", {"arc": {"title": "第一卷", "synopsis": "从异常事件开始，主角在阻力中取得阶段性证据并承担新的关系代价。"}})
    save_legacy_confirmed("summary", "default", {"story_bible": {
        "summary": "总结步骤提供的最终全书梗概。",
        "theme": "总结步骤的最终主题",
        "style_rules": "总结步骤的最终文风规则",
        "must_have_elements": ["总结新增", "共享元素"],
        "avoid_drift": ["总结禁区", "共享边界"],
    }})

    finalized = client.post(f"/api/works/{work_id}/planning-session/finalize")
    assert finalized.status_code == 200, finalized.text
    bible = finalized.json()["work"]["story_bible"]
    assert bible["summary"] == "总结步骤提供的最终全书梗概。"
    assert bible["theme"] == "总结步骤的最终主题"
    assert bible["style_rules"] == "总结步骤的最终文风规则"
    assert bible["title_interpretation"] == "选中契约解释书名并兑现承诺"
    assert bible["reader_promise"] == "选中契约承诺主动行动和清晰回报"
    assert bible["must_have_elements"] == ["契约元素", "共享元素", "设定元素", "总结新增"]
    assert bible["avoid_drift"] == ["契约禁区", "共享边界", "设定禁区", "总结禁区"]
    assert "被放弃元素" not in json.dumps(bible, ensure_ascii=False)
    assert isinstance(bible["quality_issues"], list)
    assert isinstance(bible["quality_score"], int)
    assert finalized.json()["data"]["consistency_checks"]["ok"] is True
    assert finalized.json()["data"]["coverage_checks"]["coverage"]["planned_volume_count"] == 1


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
    contract_content = contract["output"]["data"]
    for candidate in contract_content["candidates"]:
        if len(candidate.get("target_experience", "")) < 20:
            candidate["target_experience"] += "，并让每次回报都能被读者看见"
    edited_contract = client.put(
        f"/api/works/{work_id}/planning-steps/contract/contract",
        json={"content": contract_content},
    )
    assert edited_contract.status_code == 200, edited_contract.text
    confirmed_contract = client.post(f"/api/works/{work_id}/planning-steps/contract/contract/confirm", json={"candidate_index": 0})
    assert confirmed_contract.status_code == 200, confirmed_contract.text
    generate("setting")
    assert client.post(f"/api/works/{work_id}/planning-steps/setting/default/confirm", json={}).status_code == 200
    protagonist_job = generate("protagonist")
    protagonist_content = protagonist_job["output"]["data"]
    protagonist_character = protagonist_content["character"]
    if len(protagonist_character.get("biography", "")) < 120:
        biography = protagonist_character.get("biography", "")
        while len(biography) < 120:
            biography += "这段经历迫使他重新理解责任，并把每一次选择留下的关系代价带到后续行动中。"
        protagonist_character["biography"] = biography[:260]
    dramatic_core = protagonist_character.get("dramatic_core") if isinstance(protagonist_character.get("dramatic_core"), dict) else protagonist_character
    if len(dramatic_core.get("goal", "")) < 20:
        dramatic_core["goal"] = f"{dramatic_core.get('goal', '')}，并保护愿意作证的同伴"
    edited_protagonist = client.put(
        f"/api/works/{work_id}/planning-steps/protagonist/default",
        json={"content": protagonist_content},
    )
    assert edited_protagonist.status_code == 200, edited_protagonist.text
    confirmed_protagonist = client.post(f"/api/works/{work_id}/planning-steps/protagonist/default/confirm", json={})
    assert confirmed_protagonist.status_code == 200, confirmed_protagonist.text
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
    arc_job = generate("arc", "arc:1")
    arc_content = arc_job["output"]["data"]
    arc = arc_content["arc"]
    for field in ("goal", "opposition", "turning_point", "ending_state"):
        if len(arc.get(field, "")) < 20:
            arc[field] = f"{arc.get(field, '')}，并让本卷的行动后果继续推动下一阶段的冲突"
    if len(arc.get("synopsis", "")) < 120:
        synopsis = arc.get("synopsis", "")
        while len(synopsis) < 120:
            synopsis += "主角必须在本卷末留下可验证的选择后果，并让对手的升级为下一卷的公开冲突提供明确起点。"
        arc["synopsis"] = synopsis[:300]
    edited_arc = client.put(
        f"/api/works/{work_id}/planning-steps/arc/arc:1",
        json={"content": arc_content},
    )
    assert edited_arc.status_code == 200, edited_arc.text
    confirmed_arc = client.post(f"/api/works/{work_id}/planning-steps/arc/arc:1/confirm", json={})
    assert confirmed_arc.status_code == 200, confirmed_arc.text
    summary_job = generate("summary")
    summary_bible = summary_job["output"]["data"]["story_bible"]
    if len(summary_bible.get("summary", "")) < 180:
        summary = summary_bible.get("summary", "")
        while len(summary) < 180:
            summary += "这次选择会继续影响人物关系、资源代价和最终清算，主角必须在每个阶段留下无法轻易撤回的行动后果。"
        summary_bible["summary"] = summary[:350]
    if len(summary_bible.get("theme", "")) < 20:
        summary_bible["theme"] += "，并落实到人物选择和行动后果"
    wrapped_summary = client.put(
        f"/api/works/{work_id}/planning-steps/summary/default",
        json={"content": {"candidate_count": 1, "candidates": [{"story_bible": summary_bible}]}},
    )
    assert wrapped_summary.status_code == 200, wrapped_summary.text
    confirmed_summary = client.post(f"/api/works/{work_id}/planning-steps/summary/default/confirm", json={})
    assert confirmed_summary.status_code == 200, confirmed_summary.text

    finalized = client.post(f"/api/works/{work_id}/planning-session/finalize")
    assert finalized.status_code == 200, finalized.text
    work = finalized.json()["work"]
    assert work["story_bible"]["summary"] == summary_bible["summary"]
    assert work["characters"]
    assert all(item["appearance"] for item in work["characters"])
    assert all("dramatic_core" in item for item in work["characters"])
    names = [item["name"] for item in work["characters"]]
    assert len(names) == len(set(names))
    assert names.count(names[0]) == 1
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

    contract = generate("contract", "contract")
    contract_content = contract["data"]
    for candidate in contract_content["candidates"]:
        for field in ("target_experience", "protagonist_principle", "power_curve", "payoff_cadence", "power_cost"):
            if len(candidate.get(field, "")) < 20:
                candidate[field] = f"{candidate.get(field, '')}，并让每次回报都能被读者看见"
    edited_contract = client.put(
        f"/api/works/{work_id}/planning-steps/contract/contract",
        json={"content": contract_content},
    )
    assert edited_contract.status_code == 200, edited_contract.text
    confirmed_contract = client.post(f"/api/works/{work_id}/planning-steps/contract/contract/confirm", json={"candidate_index": 0})
    assert confirmed_contract.status_code == 200, confirmed_contract.text
    generate("setting")
    assert client.post(f"/api/works/{work_id}/planning-steps/setting/default/confirm", json={}).status_code == 200
    protagonist = generate("protagonist")
    protagonist_content = protagonist["data"]
    protagonist_character = protagonist_content["character"]
    biography = protagonist_character.get("biography", "")
    while len(biography) < 120:
        biography += "这段经历迫使他重新理解责任，并把每一次选择留下的关系代价带到后续行动中。"
    protagonist_character["biography"] = biography[:260]
    dramatic_core = protagonist_character.get("dramatic_core") if isinstance(protagonist_character.get("dramatic_core"), dict) else protagonist_character
    if len(dramatic_core.get("goal", "")) < 20:
        dramatic_core["goal"] = f"{dramatic_core.get('goal', '')}，并保护愿意作证的同伴"
    edited_protagonist = client.put(
        f"/api/works/{work_id}/planning-steps/protagonist/default",
        json={"content": protagonist_content},
    )
    assert edited_protagonist.status_code == 200, edited_protagonist.text
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


def test_planning_quality_has_step_specific_lengths_and_natural_language_warnings():
    short_summary = planning_checks(
        "summary",
        {"story_bible": {"summary": "完整故事梗概" * 29}},
    )
    assert any("180—350" in issue for issue in short_summary["blocking"])

    short_arc = planning_checks(
        "arc",
        {"arc": {
            "title": "第一卷",
            "goal": "找到关键证据并保护证人",
            "opposition": "对手持续制造外部压力",
            "turning_point": "主角发现责任链指向自己",
            "ending_state": "证据保住但身份暴露",
            "synopsis": "卷梗概" * 39,
        }},
    )
    assert any("卷梗概" in issue and "120—300" in issue for issue in short_arc["blocking"])

    warnings = language_risks({"text": "拉林守诚修暖，转守仓对峙。"})
    assert any("拉林守诚修暖" in warning for warning in warnings)
    assert any("转守仓对峙" in warning for warning in warnings)


def test_planning_consistency_checks_reports_blockers_with_evidence_paths():
    result = planning_consistency_checks({
        "contract": [{"selected": {"style_rules": "具体行动推动情节"}}],
        "setting": [{"story_bible": {
            "rules": {"allowed": ["夜间传送"], "forbidden": ["夜间传送"]},
            "goldfinger": {"cost": "失去记忆", "user": "林舟", "scope": "本人", "portable": False},
        }}],
        "protagonist": [{"character": {"name": "林舟"}}],
        "cast_roster": [{"characters": [{"name": "林舟"}, {"name": "林舟"}]}],
        "arc": [{"arc": {
            "sequence": 2,
            "start_chapter": 20,
            "end_chapter": 10,
            "twist": {"required_resources": ["血钥匙"], "available_resources": ["旧地图"]},
        }}],
    })

    assert result["ok"] is False
    assert any("重复主角" in issue for issue in result["blocking"])
    assert any("规则直接冲突" in issue for issue in result["blocking"])
    assert any("时间范围倒置" in issue for issue in result["blocking"])
    assert any("尚未获得" in issue for issue in result["blocking"])
    assert any(item["path"].startswith("context.") and item["suggestion"] for item in result["evidence"])


def test_planning_consistency_checks_keeps_plausibility_and_style_as_warnings():
    result = planning_consistency_checks({
        "setting": [{"story_bible": {
            "medical": {"disease": "记忆衰退", "medication": "临时药剂"},
            "ending": "公开真相并保护证人",
        }}],
        "antagonist": {"name": "陆衡"},
        "arc": [{"arc": {"sequence": 1, "goal": "守住仓库"}}],
    })

    assert result["ok"] is True
    assert any("医学因果链" in warning for warning in result["warnings"])
    assert any("对手设定" in warning for warning in result["warnings"])
    assert any("文风要求" in warning for warning in result["warnings"])
    assert result["suggestions"]


def test_planning_coverage_checks_blocks_single_confrontation_volume_for_long_book():
    result = planning_coverage_checks({
        "estimated_words": 100000,
        "average_chapter_words": 2500,
        "target_chapter_count": 40,
        "story_bible": {
            "summary": "全书总梗概包含中点反转、最低谷和最终清算。",
            "ending": "最终对峙后完成结局。",
        },
        "plot_arcs": [{"title": "第一卷", "synopsis": "主角与对手最终对峙并暂时收束。", "sequence": 1}],
    })

    assert result["ok"] is False
    assert result["full_book_ready"] is False
    assert any("不能标记为完整全书规划" in issue for issue in result["blocking"])
    assert result["coverage"]["suggested_volume_count"] == 4
    assert result["coverage"]["planned_volume_count"] == 1
    assert result["coverage"]["planned_chapters"] == 40
    assert result["coverage"]["planned_words"] == 100000
    assert result["coverage"]["summary_scope"] == "full_book"

    single_volume = planning_coverage_checks({
        "estimated_words": 100000,
        "target_chapter_count": 40,
        "single_volume_complete": True,
        "story_bible": {"summary": "全书总梗概：中点、最低谷、最终清算均在本卷完成。"},
        "plot_arcs": [{"title": "单卷完结", "synopsis": "最终对峙后完成最终清算。", "sequence": 1}],
    })
    assert not any("不能标记为完整全书规划" in issue for issue in single_volume["blocking"])
    assert single_volume["coverage"]["single_volume_complete"] is True


def test_planning_coverage_checks_reports_current_volume_and_explicit_ranges():
    result = planning_coverage_checks({
        "estimated_words": 100000,
        "average_chapter_words": 2500,
        "target_chapter_count": 40,
        "summary_scope": "current_volume",
        "story_bible": {"summary": "当前卷梗概，完成中段目标，但不是全书总梗概。"},
        "story_volumes": [{
            "sequence": 1,
            "start_chapter": 1,
            "end_chapter": 12,
            "target_words": 30000,
            "synopsis": "本卷推进阶段冲突。",
        }],
    })

    coverage = result["coverage"]
    assert coverage["planned_volume_count"] == 1
    assert coverage["planned_chapters"] == 12
    assert coverage["planned_words"] == 30000
    assert coverage["coverage_ratio"] == 0.3
    assert coverage["summary_scope"] == "current_volume"
    assert any("当前卷梗概" in warning for warning in result["warnings"])


def test_planning_put_rechecks_content_and_blocks_invalid_confirmation(client):
    created = client.post("/api/works", json={"title": "质量门保存测试"})
    work_id = created.json()["id"]
    valid = {"story_bible": {"summary": "完整故事梗概" * 30}}

    saved = client.put(
        f"/api/works/{work_id}/planning-steps/summary/default",
        json={"content": valid},
    )
    assert saved.status_code == 200
    assert saved.json()["checks"]["blocking"] == []
    assert client.post(f"/api/works/{work_id}/planning-steps/summary/default/confirm", json={}).status_code == 200

    emptied = client.put(
        f"/api/works/{work_id}/planning-steps/summary/default",
        json={"content": {"story_bible": {"summary": ""}}},
    )
    assert emptied.status_code == 200
    assert any("总梗概" in issue for issue in emptied.json()["checks"]["blocking"])

    blocked = client.post(f"/api/works/{work_id}/planning-steps/summary/default/confirm", json={})
    assert blocked.status_code == 409
    assert "总梗概" in blocked.text


def test_r1b_canonical_character_artifact_flows_into_next_step_context(client):
    created = client.post("/api/works", json={"title": "规范人物结构测试"})
    work_id = created.json()["id"]
    character = {
        "name": "顾遥",
        "role": "盟友",
        "story_function": "提供关键行动能力",
        "biography": "顾遥曾因一次错误判断失去家人，因此决定把证据送到真正能使用它的人手中。她多年在封锁区边缘替人运送物资，熟悉每条检查线的漏洞，也清楚任何一次冒险都会把无辜者拖进旧案。她后来学会把每次行动拆成可核验的步骤，在保护同伴和追查真相之间留下明确的退路，也准备承担退路失效后的关系代价。",
        "dramatic_core": {
            "goal": "在证据被销毁前把它送到公开审查的渠道并保护证人",
            "motivation": "弥补过去失误造成的伤害并让家人得到迟来的解释",
            "flaw": "过度依赖自己的退路，常常在同伴需要信任时先替他们做决定",
            "conflict": "不信任主角的犹豫，却又必须借助他的公开身份突破封锁",
        },
        "appearance": "短发贴着额角，眉尾有细疤，穿旧防水外套和硬底靴，右手腕留着一圈灼痕作为辨识标记。",
        "personality": "她先观察退路和代价，再决定是否把别人纳入自己的计划，真正承诺后又极少后退。",
        "voice": "她说话直接，习惯先报地点、时间和风险，拒绝用含糊的安慰替代行动安排。",
        "arc": "从只相信个人退路，转向愿意为共同目标承担不可撤回的风险。",
        "facets": {},
    }
    upsert_artifact(work_id, "character", "character:1", {"character": character})
    assert confirm_artifact(work_id, "character", "character:1")["steps"]
    context = planning_context_for_step(work_id, "arc")
    projected = context["character"][0]["character"]
    assert projected["dramatic_core"]["goal"] == character["dramatic_core"]["goal"]
    assert projected["arc"] == character["arc"]


def test_r1b_frontend_planning_form_uses_canonical_paths_and_safe_editors():
    source = (Path(__file__).resolve().parents[1] / "web" / "app" / "page.tsx").read_text(encoding="utf-8")
    assert 'return ["character", "dramatic_core", key]' in source
    assert '["arc", "人物弧"' in source
    assert "content.candidates.flatMap" in source
    assert "normalizePlanningContent" in source
    assert "delete character[key]" in source
    assert 'field.valueType === "number"' in source
    assert "JSON.parse(event.target.value)" in source
    assert 'await onSave("contract", activeKey, parsed, feedback)' in source
    assert 'await onSave(selectedStep, activeKey, parsed, feedback)' in source
    assert "async function generateAllCharacterBiographies(preset: string, feedback: string)" in source
    assert "JSON.stringify({ feedback, preset" in source
    assert "onGenerateAllCharacters(preset, feedback)" in source


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


def test_outline_repairs_only_failed_chapters_and_persists_repair_history(client, monkeypatch):
    created = client.post("/api/works", json={"title": "定向修复章节", "premise": "主角必须找回一封被篡改的信。"})
    work_id = created.json()["id"]
    assert client.post(f"/api/works/{work_id}/generate/setup").status_code == 200

    original_generate = engine.generate_outline
    repair_calls = []

    def generate_with_two_invalid_chapters(*args, **kwargs):
        result = original_generate(*args, **kwargs)
        result["chapters"][0]["title"] = ""
        result["chapters"][2]["phase_key"] = ""
        return result

    def repair_failed_chapters(work, failed_chapters, issues_by_chapter, profile=None, *, generation_context=None):
        repair_calls.append({"numbers": [item["chapter_no"] for item in failed_chapters], "issues": issues_by_chapter})
        repaired = []
        for entry in failed_chapters:
            chapter = dict(entry["chapter"])
            if chapter["chapter_no"] == 1:
                chapter["title"] = "修复后的开门"
            if chapter["chapter_no"] == 3:
                chapter["phase_key"] = "default"
            repaired.append(chapter)
        return {"chapters": repaired, "generation_source": "model", "repair_attempts": 1}

    monkeypatch.setattr(engine, "generate_outline", generate_with_two_invalid_chapters)
    monkeypatch.setattr(engine, "repair_outline_chapters", repair_failed_chapters)
    response = client.post(f"/api/works/{work_id}/generate/outline", json={"chapter_count": 3})

    assert response.status_code == 200, response.text
    assert repair_calls and repair_calls[0]["numbers"] == [1, 3]
    data = response.json()["data"]
    assert data["repair_count"] == 1
    history = data["repair_history"][0]
    assert history["scope"] == "chapters"
    assert history["chapter_numbers"] == [1, 3]
    assert history["original_output"][0]["title"] == ""
    assert history["issues"]["1"]
    assert response.json()["work"]["chapter_plans"][0]["title"] == "修复后的开门"
    versions = client.get(f"/api/works/{work_id}/outline-versions").json()["items"]
    assert versions[0]["request"]["repair_history"][0]["chapter_numbers"] == [1, 3]


def test_outline_repairs_the_whole_batch_only_for_structural_failure(client, monkeypatch):
    created = client.post("/api/works", json={"title": "整批结构修复", "premise": "主角必须修复一段被篡改的记忆。"})
    work_id = created.json()["id"]
    assert client.post(f"/api/works/{work_id}/generate/setup").status_code == 200

    original_generate = engine.generate_outline
    repair_calls = []

    def generate_with_missing_chapters(*args, **kwargs):
        result = original_generate(*args, **kwargs)
        result["chapters"] = result["chapters"][:1]
        return result

    def repair_whole_batch(work, failed_chapters, issues_by_chapter, profile=None, *, generation_context=None):
        repair_calls.append({"numbers": [item["chapter_no"] for item in failed_chapters], "issues": issues_by_chapter})
        result = original_generate(
            work,
            len(failed_chapters),
            profile,
            generation_context={
                **(generation_context or {}),
                "from_chapter": failed_chapters[0]["chapter_no"],
                "to_chapter": failed_chapters[-1]["chapter_no"],
            },
        )
        return {"chapters": result["chapters"], "generation_source": "fallback"}

    monkeypatch.setattr(engine, "generate_outline", generate_with_missing_chapters)
    monkeypatch.setattr(engine, "repair_outline_chapters", repair_whole_batch)
    response = client.post(f"/api/works/{work_id}/generate/outline", json={"chapter_count": 3})

    assert response.status_code == 200, response.text
    assert repair_calls and repair_calls[0]["numbers"] == [1, 2, 3]
    assert "batch" in response.json()["data"]["repair_history"][0]["scope"]
    assert len(response.json()["work"]["chapter_plans"]) == 3


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
    content = {"candidates": [
        {"title": "方向一", "target_experience": "明确回报并让每次选择都留下可见后果，并推动下一步行动", "body": "具体行动推动情节并保持明确回报节奏和人物选择" * 6},
        {"title": "方向二", "body": "具体行动推动情节并保持明确回报节奏和人物选择" * 6},
        {"title": "方向三", "body": "具体行动推动情节并保持明确回报节奏和人物选择" * 6},
    ]}
    saved = client.put(f"/api/works/{work_id}/planning-steps/contract/contract", json={"content": content})
    assert saved.status_code == 200
    assert saved.json()["status"] == "draft"
    confirmed = client.post(f"/api/works/{work_id}/planning-steps/contract/contract/confirm", json={"candidate_index": 0})
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["current_step"] == "setting"
