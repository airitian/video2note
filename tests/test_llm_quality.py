"""LLM 质量管线（2026-11-21）：分风格温度 / 术语预算 / 忠实合并 / 质检闭环。

全部用假 chat 驱动，不打真实接口。
"""
import json

from core import llm


# ---------- 分风格温度 ----------

def test_style_temperature_defaults():
    assert llm._style_temperature("general") == 0.1
    assert llm._style_temperature("star") == 0.35
    assert llm._style_temperature("article") == 0.65
    assert llm._style_temperature("unknown-style") == 0.3


def test_removed_styles_are_gone():
    # 2026-11-21 按用户要求下线：仅清洗润色（clean）、结构化笔记（note）
    assert set(llm.STYLES) == {"general", "digest", "article", "star"}
    # 传入已下线风格时回退到默认模板而不是报错
    assert llm.STYLES.get("clean", llm.STYLES[llm.DEFAULT_STYLE]) is llm.STYLES["general"]
    assert llm._style_temperature("digest") == 0.3


def test_style_temperature_user_override_wins():
    f = llm.SETTINGS_FILE
    f.write_text(json.dumps({"llm_temperature": "0.9"}), encoding="utf-8")
    try:
        assert llm._style_temperature("clean") == 0.9
        assert llm._style_temperature("article") == 0.9
    finally:
        f.unlink(missing_ok=True)


# ---------- 术语预算 ----------

def test_terms_budget_scales_with_length():
    assert llm._terms_budget(3000) == 8000          # 地板
    assert llm._terms_budget(20000) == 10000        # 一半
    assert llm._terms_budget(100000) == 16000       # 上限


# ---------- 生成内质检已下线；质检改为生成后独立报告 ----------

def test_polish_short_path_makes_one_call_and_returns_note(monkeypatch):
    # 生成链路里不再有质检/修订：一次调用直接交付成稿，不会被整篇重写
    calls = []

    def fake_chat(system, user, **kw):
        calls.append(user)
        return "整理好的成稿"

    monkeypatch.setattr(llm, "chat", fake_chat)
    out = llm.polish("一小段原文", {"title": "标题"}, style="general",
                     progress=lambda m: None)
    assert out == "整理好的成稿"
    assert len(calls) == 1
    assert "质检员" not in calls[0]


def test_review_note_parses_verdict(monkeypatch):
    def fake_chat(system, user, **kw):
        assert "质检员" in user
        return '{"fit": "不符合", "problems": ["删太多了"], "suggestions": ["按原文补回"]}'

    monkeypatch.setattr(llm, "chat", fake_chat)
    r = llm.review_note("原文", "成稿", "general", "judge-model")
    assert r["fit"] == "不符合"
    assert r["problems"] == ["删太多了"]
    assert r["suggestions"] == ["按原文补回"]


def test_review_note_failure_returns_unrated(monkeypatch):
    def boom(system, user, **kw):
        raise RuntimeError("endpoint down")

    monkeypatch.setattr(llm, "chat", boom)
    r = llm.review_note("原文", "成稿", "general", None)
    assert r["fit"] == "未评"
    assert r["problems"] and "质检调用失败" in r["problems"][0]


def test_review_note_garbage_returns_unrated(monkeypatch):
    monkeypatch.setattr(llm, "chat", lambda s, u, **kw: "不是 JSON")
    r = llm.review_note("原文", "成稿", "digest", None)
    assert r["fit"] == "未评"


# ---------- temperature 适配 ----------

def test_kimi_models_omit_temperature():
    # Kimi K3 经 DashScope 路由不接受 temperature 参数，传了会 400
    assert llm._model_omits_temperature("moonshotai/Kimi-K3.1-Pro:DashScope")
    assert llm._model_omits_temperature("moonshotai/Kimi-K2-Instruct")
    assert not llm._model_omits_temperature("deepseek-ai/DeepSeek-V4-Pro-0813")
    assert not llm._model_omits_temperature("Qwen/Qwen3.5-397B-A17B")
    assert not llm._model_omits_temperature("")


# ---------- 签名 ----------

def test_signature_depends_on_style_temperature():
    # general(0.2) 与 article(0.65) 温度不同 → 同原文签名必须不同
    assert llm.signature("abc", "general") != llm.signature("abc", "article")
    assert llm.signature("abc", "general") == llm.signature("abc", "general")


# ---------- 长稿 general（整理文稿）：清洗稿直拼 ----------

def test_polish_long_general_is_verbatim_cleaned(monkeypatch):
    raw = "\n\n".join(
        f"第{i}段的完整内容，包含具体事实与数字{i}。" + "这里是该段的展开论述，用来把段落撑到足够长度，确保整篇稿子超过短文本阈值，走长稿全链路。" * 2
        for i in range(30))
    assert len(raw) > llm.SHORT_LIMIT

    def fake_chat(system, user, **kw):
        if "提取所有专有名词" in user:
            return "术语甲、术语乙"
        if "质检员" in user:
            return '{"coverage": 100, "fabrications": [], "missing": []}'
        return user.split("原始文本：\n", 1)[1].strip()

    monkeypatch.setattr(llm, "chat", fake_chat)
    out = llm.polish(raw, {"title": "标题"}, style="general",
                     progress=lambda m: None)
    assert out.startswith("# 标题\n\n")
    for i in range(30):
        assert f"包含具体事实与数字{i}" in out


