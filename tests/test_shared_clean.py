"""共享清洗产物 + 清洗文稿风格（2026-11-23）。

设计：一篇稿子的清洗只跑一次（ensure_clean 落盘到 t.clean_cache），
清洗文稿 = 产物本身、整理文稿 = 产物加标题、其余风格只剩 compose。
全部用假 LLM 驱动，不打真实接口。
"""
import time

from core import llm, main, pipeline, qiniu_archive, store


def _task(text="原始转写内容", tid="sc-task"):
    t = store.Task(id=tid)
    t.transcript = text
    t.meta = {"title": "标题"}
    t.status = "transcribed"
    t.stage = "transcribed"
    return t


def _seed_cache(t, text="清洗后正文", model="m/Clean"):
    t.clean_cache = {"text": text, "terms": "术语甲",
                     "sig": llm.clean_signature(t.transcript),
                     "model": model, "seconds": 1.0, "ts": time.time()}
    return t.clean_cache


def _quiet(monkeypatch):
    """关掉 _polish_body 尾部的云端备份旁路，避免测试碰真实归档逻辑。"""
    monkeypatch.setattr(pipeline, "_schedule_backup", lambda t: None)
    monkeypatch.setattr(qiniu_archive, "upload_note", lambda *a, **k: None)


# ---------- ensure_clean：缓存命中不调 LLM、原文变即失效 ----------

def test_ensure_clean_caches_and_reuses(monkeypatch):
    calls = []

    def fake_clean(raw, meta, progress=None, model=None):
        calls.append(raw)
        return "清洗后正文", "术语甲、术语乙"

    monkeypatch.setattr(llm, "clean_transcript", fake_clean)
    t = _task()
    c1 = pipeline.ensure_clean(t, model="m/X")
    c2 = pipeline.ensure_clean(t, model="m/Y")   # 再来一次：应命中缓存
    assert len(calls) == 1                        # 只洗了一次
    assert c1["text"] == c2["text"] == "清洗后正文"
    assert c1["terms"] == "术语甲、术语乙"
    assert c1["model"] == "m/X"                   # 记录实际清洗所用模型
    assert t.clean_cache["sig"] == llm.clean_signature(t.transcript)


def test_ensure_clean_invalidated_by_transcript_change(monkeypatch):
    calls = []
    monkeypatch.setattr(
        llm, "clean_transcript",
        lambda raw, meta, progress=None, model=None:
        (calls.append(raw), ("洗后：" + raw[:4], ""))[1])
    t = _task("第一版原文")
    pipeline.ensure_clean(t)
    sig_before = pipeline.polish_signature(t, "cleaned")
    t.transcript = "第二版原文改了"
    assert pipeline.clean_cache_valid(t) is None      # 签名不匹配 = 失效
    assert pipeline.polish_signature(t, "cleaned") != sig_before
    pipeline.ensure_clean(t)
    assert len(calls) == 2                            # 重新清洗


def test_ensure_clean_force_reclean(monkeypatch):
    calls = []
    monkeypatch.setattr(
        llm, "clean_transcript",
        lambda raw, meta, progress=None, model=None:
        (calls.append(1), ("正文", ""))[1])
    t = _task()
    pipeline.ensure_clean(t)
    pipeline.ensure_clean(t, force=True)
    assert len(calls) == 2


# ---------- 清洗文稿风格：成稿就是清洗产物 ----------

def test_cleaned_style_note_is_clean_product(monkeypatch):
    _quiet(monkeypatch)
    monkeypatch.setattr(
        llm, "clean_transcript",
        lambda raw, meta, progress=None, model=None: ("清洗后正文", "术语甲"))
    monkeypatch.setattr(pipeline, "pick_generation_model", lambda t: "m/X")
    t = _task()
    pipeline._polish_body(t, "cleaned")
    v = t.variants["cleaned"]
    assert v["note"] == "清洗后正文"               # 不加标题、就是产物本身
    assert v["info"]["model"] == "m/X"
    assert v["info"]["chars"] == len("清洗后正文")
    assert v["sig"] == llm.clean_signature(t.transcript)


# ---------- 整理文稿：清洗命中后零额外 LLM 调用 ----------

def test_general_zero_llm_after_clean(monkeypatch):
    _quiet(monkeypatch)
    t = _task()
    _seed_cache(t)

    def boom(*a, **k):
        raise AssertionError("命中清洗缓存后不应再调 LLM / 挑模型")

    monkeypatch.setattr(llm, "clean_transcript", boom)
    monkeypatch.setattr(llm, "polish", boom)
    monkeypatch.setattr(llm, "chat", boom)
    monkeypatch.setattr(pipeline, "pick_generation_model", boom)
    pipeline._polish_body(t, "general")
    v = t.variants["general"]
    assert v["note"] == "# 标题\n\n清洗后正文"      # 产物加一行标题
    assert v["info"]["model"] == "m/Clean"          # 记清洗实际所用模型


# ---------- 精简文稿：走共享清洗后只剩 compose ----------

def test_digest_only_composes_on_shared_clean(monkeypatch):
    _quiet(monkeypatch)
    t = _task()
    _seed_cache(t)
    seen = {}

    def fake_compose(cleaned, meta, style, progress=None, model=None):
        seen["cleaned"] = cleaned
        seen["style"] = style
        return "精简稿"

    def boom(*a, **k):
        raise AssertionError("不应走老路径/重新清洗")

    monkeypatch.setattr(llm, "compose_from_cleaned", fake_compose)
    monkeypatch.setattr(llm, "clean_transcript", boom)
    monkeypatch.setattr(llm, "polish", boom)
    monkeypatch.setattr(pipeline, "pick_generation_model", lambda t: "m/Y")
    pipeline._polish_body(t, "digest")
    assert seen == {"cleaned": "清洗后正文", "style": "digest"}
    assert t.variants["digest"]["note"] == "精简稿"
    assert t.variants["digest"]["info"]["model"] == "m/Y"


def test_digest_falls_back_when_clean_fails(monkeypatch):
    _quiet(monkeypatch)

    def bad_clean(raw, meta, progress=None, model=None):
        raise RuntimeError("endpoint down")

    monkeypatch.setattr(llm, "clean_transcript", bad_clean)
    monkeypatch.setattr(
        llm, "polish",
        lambda raw, meta, style, progress=None, model=None: "老路径稿")
    monkeypatch.setattr(pipeline, "pick_generation_model", lambda t: "m/Z")
    t = _task()
    pipeline._polish_body(t, "digest")
    assert t.variants["digest"]["note"] == "老路径稿"   # 兜底：仍能生成


# ---------- llm 层：clean_transcript / compose / 签名 ----------

def test_clean_transcript_short_single_call(monkeypatch):
    calls = []

    def fake_chat(system, user, **kw):
        calls.append(user)
        return "洗好的短稿"

    monkeypatch.setattr(llm, "chat", fake_chat)
    cleaned, terms = llm.clean_transcript("一小段原文", {"title": "标题"})
    assert cleaned == "洗好的短稿"
    assert terms == ""                    # 短稿单块清洗不抽术语
    assert len(calls) == 1


def test_clean_transcript_long_extracts_terms_and_chunks(monkeypatch):
    raw = "\n\n".join(
        f"第{i}段的完整内容，包含具体事实与数字{i}。" + "展开论述撑长度。" * 10
        for i in range(36))
    assert len(raw) > llm.SHORT_LIMIT

    def fake_chat(system, user, **kw):
        if "提取所有专有名词" in user:
            return "术语甲、术语乙"
        return user.split("原始文本：\n", 1)[1].strip()

    monkeypatch.setattr(llm, "chat", fake_chat)
    cleaned, terms = llm.clean_transcript(raw, {"title": "标题"})
    assert terms == "术语甲、术语乙"
    for i in range(36):
        assert f"数字{i}" in cleaned


def test_compose_general_stitches_title_without_llm(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("general compose 不应调 LLM")

    monkeypatch.setattr(llm, "chat", boom)
    out = llm.compose_from_cleaned("清洗正文", {"title": "标题"}, "general")
    assert out == "# 标题\n\n清洗正文"


def test_clean_signature_stability():
    assert llm.clean_signature("abc") == llm.clean_signature("abc")
    assert llm.clean_signature("abc") != llm.clean_signature("abd")


# ---------- 风格注册：质检 rubric / 接口白名单 / 自动生成顺序 ----------

def test_cleaned_registered_everywhere():
    assert "cleaned" in llm.STYLE_RUBRICS
    assert "cleaned" in main.STYLES
    assert main._clean_style("cleaned") == "cleaned"
    assert main._clean_style("bogus") == "general"
    assert pipeline.AUTO_STYLES[0] == "cleaned"          # 清洗最先
    assert set(pipeline.AUTO_STYLES) == {
        "cleaned", "general", "digest", "article", "star"}
