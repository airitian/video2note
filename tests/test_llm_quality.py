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


# ---------- 质检闭环 ----------

def _judge_chat(verdict_json, repaired="REPAIRED-TEXT"):
    def fake_chat(system, user, **kw):
        if "质检员" in user:
            return verdict_json
        if "质检发现" in user:
            return repaired
        raise AssertionError(f"unexpected chat call: {user[:40]}")
    return fake_chat


def test_judge_pass_returns_original(monkeypatch):
    monkeypatch.setattr(llm, "chat", _judge_chat(
        '{"coverage": 95, "fabrications": [], "missing": []}'))
    out = llm._judge_and_repair("原文", "成稿", "general", "t", None, lambda m: None)
    assert out == "成稿"


def test_judge_fail_triggers_one_repair(monkeypatch):
    monkeypatch.setattr(llm, "chat", _judge_chat(
        '{"coverage": 60, "fabrications": ["编了个数据"], "missing": ["漏了结论"]}'))
    out = llm._judge_and_repair("原文", "成稿", "general", "t", None, lambda m: None)
    assert out == "REPAIRED-TEXT"


def test_judge_garbage_never_blocks(monkeypatch):
    monkeypatch.setattr(llm, "chat", _judge_chat("不是 JSON"))
    out = llm._judge_and_repair("原文", "成稿", "general", "t", None, lambda m: None)
    assert out == "成稿"


def test_repair_must_not_drop_star_sections(monkeypatch):
    # 修订稿把固定章节修丢了 → 退回原稿，不能为了补内容牺牲格式
    original = "# 问题\n\n## 内容判断\n\n判断。\n\n## 优化后的回答\n\n回答。"
    monkeypatch.setattr(llm, "chat", _judge_chat(
        '{"coverage": 50, "fabrications": [], "missing": ["漏了短版"]}',
        repaired="# 问题\n\n修订后的自由正文，没有章节了。"))
    out = llm._judge_and_repair("原文", original, "star", "t", None, lambda m: None)
    assert out == original


def test_judge_can_be_disabled(monkeypatch):
    monkeypatch.setenv("V2N_POLISH_JUDGE", "0")

    def boom(system, user, **kw):
        raise AssertionError("judge 不应被调用")
    monkeypatch.setattr(llm, "chat", boom)
    out = llm._judge_and_repair("原文", "成稿", "general", "t", None, lambda m: None)
    assert out == "成稿"


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


