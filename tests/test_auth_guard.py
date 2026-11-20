"""鉴权守卫（FastAPI TestClient + 临时 V2N_DATA_DIR）。

覆盖问题 1 的核心断言：配了令牌后，任务列表/详情/媒体/删除无令牌
一律 401；带令牌 200；公开端点（health、auth/state）保持公开。
"""
import pytest
from fastapi.testclient import TestClient

from conftest import ADMIN
from core import store
from core.main import app

client = TestClient(app)


def test_public_endpoints_stay_open():
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/auth/state").status_code == 200


def test_task_list_requires_token():
    r = client.get("/api/tasks")
    assert r.status_code == 401
    assert r.json()["code"] == "NEED_TOKEN"


def test_task_list_with_token_ok():
    r = client.get("/api/tasks", headers=ADMIN)
    assert r.status_code == 200
    assert isinstance(r.json(), list)   # 列表接口返回摘要数组（既有契约）


def test_task_list_wrong_token_401():
    r = client.get("/api/tasks", headers={"X-Access-Token": "nope"})
    assert r.status_code == 401


def test_task_list_cookie_token_ok():
    c = TestClient(app)
    c.cookies.set("v2n_token", "test-admin-token")
    assert c.get("/api/tasks").status_code == 200


def test_detail_media_delete_require_token():
    t = store.create("https://example.com/v", "douyin", {})
    tid = t.id
    assert client.get(f"/api/tasks/{tid}").status_code == 401
    assert client.get(f"/api/tasks/{tid}", headers=ADMIN).status_code == 200
    # media：守卫先于 404 —— 无令牌 401，有令牌才走到文件不存在的 404
    assert client.get(f"/api/media/{tid}/video.mp4").status_code == 401
    assert client.get(f"/api/media/{tid}/video.mp4",
                      headers=ADMIN).status_code == 404
    assert client.delete(f"/api/tasks/{tid}").status_code == 401
    assert client.delete(f"/api/tasks/{tid}", headers=ADMIN).status_code == 200


def test_settings_requires_admin():
    assert client.get("/api/settings").status_code in (401, 403)


def test_search_filter():
    a = store.create("https://example.com/1", "local", {})
    a.meta = {"title": "猫咪视频标题甲"}
    store.save(a)
    b = store.create("https://example.com/2", "local", {})
    b.meta = {"title": "狗狗视频标题乙"}
    store.save(b)
    r = client.get("/api/tasks", params={"q": "猫咪"}, headers=ADMIN)
    assert r.status_code == 200
    titles = [(x.get("meta") or {}).get("title", "") for x in r.json()]
    assert any("猫咪" in x for x in titles)
    assert not any("狗狗" in x for x in titles)


def test_batch_create_reports_ssrf_per_item():
    r = client.post("/api/tasks/batch",
                    json={"urls": ["http://127.0.0.1:9/evil", ""]},
                    headers=ADMIN)
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 2 and body["created"] == 0
    assert body["results"][0]["ok"] is False
    assert "内网" in body["results"][0]["error"]


def test_patch_transcript_clears_variants():
    t = store.create("https://example.com/v2", "douyin", {})
    t.transcript = "旧转写"
    t.note = "旧成稿"
    t.variants = {"detailed": "旧变体"}
    t.note_sig = "sig"
    store.save(t)
    r = client.patch(f"/api/tasks/{t.id}/transcript",
                     json={"transcript": "修订后的转写"}, headers=ADMIN)
    assert r.status_code == 200
    body = r.json()
    assert body["transcript"] == "修订后的转写"
    assert body["note"] == ""
    assert body["variants"] == {}
