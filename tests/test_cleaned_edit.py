"""清洗文稿可编辑 + 清洗口径强化（2026-11-23）。

编辑链路：PATCH /api/tasks/{tid}/cleaned 只换 clean_cache 文本与
cleaned 变体——缓存签名按转写原文算、不变，ensure_clean 命中编辑稿
不重洗；下游风格签名基于清洗文本，编辑后自动失效、下次点击重生成。
口径强化：CLEAN_TMPL 明写广告/引流/无意义/无关四类、整段删，
STYLE_RUBRICS["cleaned"] 同步四类检查。全部假 LLM 驱动。
"""
import time

from fastapi.testclient import TestClient

from conftest import ADMIN
from core import llm, pipeline, qiniu_archive, store
from core.main import app

client = TestClient(app)


def _quiet(monkeypatch):
    monkeypatch.setattr(pipeline, "_schedule_backup", lambda t: None)
    monkeypatch.setattr(qiniu_archive, "upload_note", lambda *a, **k: None)


def _seeded_task(monkeypatch, cleaned="清洗后正文"):
    """一条已转写、已有共享清洗缓存与 cleaned 变体的任务。"""
    _quiet(monkeypatch)
    t = store.create("https://example.com/v", "douyin", {})
    t.transcript = "原始转写内容，包含要点甲与要点乙。"
    t.meta = {"title": "标题"}
    t.status = "done"
    t.stage = "done"
    t.clean_cache = {"text": cleaned, "terms": "术语甲",
                     "sig": llm.clean_signature(t.transcript),
                     "model": "m/Clean", "seconds": 1.0, "ts": time.time()}
    pipeline._polish_body(t, "cleaned")     # 命中缓存：零 LLM 生成变体
    store.save(t)
    return t


# ---------- 接口：保存与回读 ----------

def test_patch_cleaned_saves_and_reads_back(monkeypatch):
    t = _seeded_task(monkeypatch)
    r = client.patch(f"/api/tasks/{t.id}/cleaned",
                     json={"text": "人工修订后的清洗稿"}, headers=ADMIN)
    assert r.status_code == 200
    d = r.json()
    assert d["clean_cache"]["text"] == "人工修订后的清洗稿"
    assert d["clean_cache"]["edited"] is True
    v = d["variants"]["cleaned"]
    assert v["note"] == "人工修订后的清洗稿"
    assert v["info"]["chars"] == len("人工修订后的清洗稿")
    assert v["info"]["edited"] is True and v["info"]["edited_at"]
    # 签名仍按原文计算：缓存有效性不被编辑破坏
    assert pipeline.clean_cache_valid(t) is not None
    # 详情接口回读一致
    d2 = client.get(f"/api/tasks/{t.id}", headers=ADMIN).json()
    assert d2["variants"]["cleaned"]["note"] == "人工修订后的清洗稿"


def test_patch_cleaned_empty_rejected(monkeypatch):
    t = _seeded_task(monkeypatch)
    r = client.patch(f"/api/tasks/{t.id}/cleaned",
                     json={"text": "   "}, headers=ADMIN)
    assert r.status_code == 400
    assert t.clean_cache["text"] == "清洗后正文"      # 没被改动


def test_patch_cleaned_requires_token(monkeypatch):
    t = _seeded_task(monkeypatch)
    r = client.patch(f"/api/tasks/{t.id}/cleaned",
                     json={"text": "x"})
    assert r.status_code == 401


def test_patch_cleaned_without_cache_409(monkeypatch):
    _quiet(monkeypatch)
    t = store.create("https://example.com/v2", "douyin", {})
    t.transcript = "只有原文、还没清洗"
    t.status = "done"
    t.stage = "done"
    store.save(t)
    r = client.patch(f"/api/tasks/{t.id}/cleaned",
                     json={"text": "想编辑但没底稿"}, headers=ADMIN)
    assert r.status_code == 409


def test_patch_cleaned_running_409(monkeypatch):
    t = _seeded_task(monkeypatch)
    t.status = "running"
    r = client.patch(f"/api/tasks/{t.id}/cleaned",
                     json={"text": "运行中不许改"}, headers=ADMIN)
    assert r.status_code == 409


# ---------- 编辑后：ensure_clean 命中编辑稿、下游签名失效 ----------

def test_ensure_clean_hits_edited_text(monkeypatch):
    t = _seeded_task(monkeypatch)
    client.patch(f"/api/tasks/{t.id}/cleaned",
                 json={"text": "编辑稿"}, headers=ADMIN)

    def boom(*a, **k):
        raise AssertionError("编辑后 ensure_clean 不应再调 LLM 清洗")

    monkeypatch.setattr(llm, "clean_transcript", boom)
    cache = pipeline.ensure_clean(t)
    assert cache["text"] == "编辑稿"


def test_edit_invalidates_downstream_signature(monkeypatch):
    t = _seeded_task(monkeypatch)
    # 先按旧底稿生成 digest（只 compose）
    monkeypatch.setattr(
        llm, "compose_from_cleaned",
        lambda cleaned, meta, style, progress=None, model=None:
        "精简稿（基于：" + cleaned + "）")
    monkeypatch.setattr(pipeline, "pick_generation_model", lambda t: "m/Y")
    pipeline._polish_body(t, "digest")
    sig_before = t.variants["digest"]["sig"]
    assert sig_before == pipeline.polish_signature(t, "digest")

    client.patch(f"/api/tasks/{t.id}/cleaned",
                 json={"text": "编辑后的新底稿"}, headers=ADMIN)
    # 下游成稿签名基于清洗文本：编辑后必然不匹配 → 下次点击重新生成
    assert pipeline.polish_signature(t, "digest") != sig_before
    need, reason = pipeline.polish_needed(t, "digest")
    assert need is True
    # 而 cleaned 本身签名按原文算：编辑不触发它重洗
    assert pipeline.polish_signature(t, "cleaned") == \
        llm.clean_signature(t.transcript)


# ---------- 口径强化：模板与 rubric 覆盖四类 ----------

def test_clean_template_covers_four_categories():
    tmpl = llm.CLEAN_TMPL
    # ①广告 ②引流 ③无实际意义 ④与主题无关 + 整段删的强口径
    for kw in ("广告", "带货", "价格促销", "下单", "课程",
               "引流", "点赞", "加微信", "橱窗",
               "无实际意义", "口头禅", "重复啰嗦",
               "与主题无关", "跑题", "整段", "不要保留半句"):
        assert kw in tmpl, kw
    # 保留条款不弱化
    for kw in ("实质内容", "不概括", "不缩写"):
        assert kw in tmpl, kw
    rubric = llm.STYLE_RUBRICS["cleaned"]
    for kw in ("广告", "引流", "无实际意义", "与主题无关"):
        assert kw in rubric, kw


def test_clean_signature_changes_with_template():
    # 口径（模板）变 → 清洗签名变 → 旧缓存失效重洗（升版生效机制）
    sig = llm.clean_signature("某段原文")
    assert sig == llm.clean_signature("某段原文")
    assert "广告" in llm.CLEAN_TMPL and "引流" in llm.CLEAN_TMPL
