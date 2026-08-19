import os
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


DB_PATH = Path(tempfile.gettempdir()) / "ai-novel-generator-test.db"
if DB_PATH.exists():
    DB_PATH.unlink()
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"
os.environ.pop("LLM_API_KEY", None)

from app.main import app  # noqa: E402
from app.services.context_builder import build_context  # noqa: E402
from app.services.generation_jobs import run_worker_once  # noqa: E402
from app.services.novel_engine import engine  # noqa: E402


@pytest.fixture()
def client():
    with TestClient(app) as test_client:
        yield test_client


def test_health(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


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
    extraction = chapter.json()["state_extraction"]
    assert extraction["status"] == "pending"
    assert extraction["chapter_version_id"]
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
    assert rerun.json()["id"] != extraction["id"]
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
    assert saved.json()["state_extraction"]["status"] == "pending"

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

    def fake_extract(work, chapter):
        return {
            "characters": [{
                "character_name": "主角",
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
    extraction = chapter.json()["state_extraction"]
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
