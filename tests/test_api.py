import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


DB_PATH = Path("/tmp/ai-novel-generator-test.db")
if DB_PATH.exists():
    DB_PATH.unlink()
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"
os.environ.pop("LLM_API_KEY", None)

from app.main import app  # noqa: E402


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

    saved = client.patch(
        f"/api/works/{work_id}/chapters/1",
        json={"content": "主角走进旧档案室。关键对手已经在那里等他。"},
    )
    assert saved.status_code == 200
    assert saved.json()["work"]["chapters"][0]["content"].startswith("主角")

    detail = client.get(f"/api/works/{work_id}")
    assert detail.status_code == 200
    assert detail.json()["chapters"][0]["chapter_no"] == 1

